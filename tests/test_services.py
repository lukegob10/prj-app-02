from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, RequestFactory, override_settings

from agora.core.models import User
from agora.db.backends.treasury_oracle.base import treasury_dependency_path
from agora.log_redaction import ContentRequestTargetFilter, redact_content_request_target
from agora.portal import views as portal_views
from agora.rendering.authorization import RenderCredential
from agora.service_entrypoint import configure_django_settings


class ServiceEntrypoint(Protocol):
    application: object


class ContentComposition(Protocol):
    SECRET_KEY: str
    ALLOWED_HOSTS: list[str]
    INSTALLED_APPS: list[str]
    MIGRATION_MODULES: dict[str, str | None]
    MIDDLEWARE: list[str]
    LOGGING: dict[str, object]


def test_django_never_trusts_raw_proxy_headers() -> None:
    assert settings.SECURE_PROXY_SSL_HEADER is None
    assert settings.USE_X_FORWARDED_HOST is False


def test_portal_static_serving_uses_asgi_native_middleware() -> None:
    security_index = settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")

    assert "servestatic" in settings.INSTALLED_APPS
    assert settings.MIDDLEWARE[security_index + 1] == "servestatic.middleware.ServeStaticMiddleware"
    assert settings.STORAGES["staticfiles"]["BACKEND"] == (
        "servestatic.storage.CompressedManifestStaticFilesStorage"
        if settings.AGORA_ENVIRONMENT == "production"
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    )
    assert settings.SERVESTATIC_USE_FINDERS is (settings.AGORA_ENVIRONMENT != "production")
    assert settings.SERVESTATIC_USE_MANIFEST is (settings.AGORA_ENVIRONMENT == "production")
    assert settings.SERVESTATIC_USE_STATIC_ROOT is (settings.AGORA_ENVIRONMENT == "production")


@override_settings(SECURE_SSL_REDIRECT=True)
def test_raw_forwarded_proto_cannot_spoof_https(client: Client) -> None:
    response = client.get(
        "/login/",
        HTTP_HOST="localhost",
        HTTP_X_FORWARDED_PROTO="https",
    )

    assert response.status_code == 301
    assert response.headers["Location"] == "https://localhost/login/"


@override_settings(SECURE_SSL_REDIRECT=True)
def test_normalized_https_scope_is_honored_without_raw_header_trust(client: Client) -> None:
    response = client.get("/login/", HTTP_HOST="localhost", secure=True)

    assert response.status_code == 200


def test_forwarded_host_never_replaces_the_canonical_host(client: Client) -> None:
    response = client.get(
        "/login/",
        HTTP_HOST="untrusted.example",
        HTTP_X_FORWARDED_HOST="localhost",
    )

    assert response.status_code == 400


@pytest.mark.smoke
def test_portal_is_runnable(client: Client) -> None:
    response = client.get("/", HTTP_HOST="localhost")

    assert response.status_code == 200
    assert b"Share dashboards without giving up control" in response.content
    assert b"Dashboard code runs outside the trusted portal" in response.content
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-src http://127.0.0.1:8001" in response.headers["Content-Security-Policy"]


@pytest.mark.smoke
def test_portal_rejects_the_content_hostname(client: Client) -> None:
    response = client.get("/", HTTP_HOST="127.0.0.1")

    assert response.status_code == 400


@pytest.mark.smoke
@override_settings(
    ROOT_URLCONF="agora.urls.content",
    ALLOWED_HOSTS=["127.0.0.1"],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "agora.middleware.ContentSecurityHeadersMiddleware",
    ],
)
def test_content_service_starts_fail_closed(client: Client) -> None:
    for path in ("/", "/arbitrary/path", "/artifact/1/dashboard.html", "/csv/report.csv"):
        response = client.get(path, HTTP_HOST="127.0.0.1")

        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts" in response.headers["Content-Security-Policy"]
        assert (
            "frame-ancestors http://localhost:8000" in response.headers["Content-Security-Policy"]
        )

    assert client.post("/artifact/1/dashboard.html", HTTP_HOST="127.0.0.1").status_code == 404


@pytest.mark.smoke
def test_service_entrypoints_and_content_composition_import() -> None:
    portal_module = cast(ServiceEntrypoint, import_module("agora.asgi"))
    assert callable(portal_module.application)

    content_settings = cast(ContentComposition, import_module("agora.settings.content"))
    assert content_settings.ALLOWED_HOSTS == ["127.0.0.1"]
    assert content_settings.INSTALLED_APPS == ["agora.core.apps.CoreConfig"]
    assert content_settings.MIGRATION_MODULES == {"persistence": None}
    assert content_settings.SECRET_KEY != settings.SECRET_KEY
    assert all("session" not in middleware.lower() for middleware in content_settings.MIDDLEWARE)
    assert content_settings.LOGGING["filters"] == {
        "redact_content_request_target": {"()": "agora.log_redaction.ContentRequestTargetFilter"}
    }


def test_service_entrypoint_settings_selection_fails_closed() -> None:
    unset: dict[str, str] = {}
    configure_django_settings(unset, "agora.settings.content")
    assert unset["DJANGO_SETTINGS_MODULE"] == "agora.settings.content"

    matching = {"DJANGO_SETTINGS_MODULE": "agora.settings.portal"}
    configure_django_settings(matching, "agora.settings.portal")

    conflicting = {"DJANGO_SETTINGS_MODULE": "agora.settings.portal"}
    with pytest.raises(ImproperlyConfigured, match="conflicts"):
        configure_django_settings(conflicting, "agora.settings.content")


def test_logout_invalidates_session_even_when_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = RequestFactory().post("/logout/")
    request.user = User(id=uuid4(), soeid="LOGOUT.AUDIT.FAILURE")
    audit = Mock(side_effect=RuntimeError("simulated audit outage"))
    invalidate = Mock()
    monkeypatch.setattr(portal_views, "record_logout", audit)
    monkeypatch.setattr(portal_views, "logout", invalidate)

    with caplog.at_level("ERROR", logger="agora.portal.views"):
        response = portal_views.logout_view(request)

    assert response.status_code == 302
    assert response.headers["Location"] == "/login/"
    audit.assert_called_once_with(request.user)
    invalidate.assert_called_once_with(request)
    assert "session was still invalidated" in caplog.text
    assert "LOGOUT.AUDIT.FAILURE" not in caplog.text


def test_render_credential_repr_redacts_plaintext_token() -> None:
    credential = RenderCredential(
        token="plain-text-bearer",
        audience="preview",
        expires_at=datetime.now(UTC),
    )

    assert credential.token not in repr(credential)


def test_content_request_log_filter_redacts_bearer_path_segments() -> None:
    token = "plain-text-bearer"
    record = logging.LogRecord(
        "django.request",
        logging.ERROR,
        __file__,
        1,
        "%s: %s",
        ("Internal Server Error", f"/render/preview/{token}/data.csv?ignored=1"),
        None,
    )

    assert ContentRequestTargetFilter().filter(record) is True
    assert token not in record.getMessage()
    assert "/render/preview/[REDACTED]/data.csv" in record.getMessage()
    assert redact_content_request_target(f"/render/viewer/{token}/") == (
        "/render/viewer/[REDACTED]/"
    )


@pytest.mark.smoke
@pytest.mark.django_db
def test_oracle_connection_uses_the_expected_vendor() -> None:
    package_path = treasury_dependency_path()
    assert Path(sys.prefix).resolve() in package_path.parents
    assert package_path.parent.name == "treasury_analytics"
    assert connection.vendor == "oracle"
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)
