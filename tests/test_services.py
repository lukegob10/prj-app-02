from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client, override_settings

from agora.db.backends.treasury_oracle.base import treasury_dependency_path


class ServiceEntrypoint(Protocol):
    application: object


class ContentComposition(Protocol):
    SECRET_KEY: str
    ALLOWED_HOSTS: list[str]
    INSTALLED_APPS: list[str]
    MIDDLEWARE: list[str]


@pytest.mark.smoke
def test_portal_is_runnable(client: Client) -> None:
    response = client.get("/", HTTP_HOST="portal.agora.test")

    assert response.status_code == 200
    assert b"Share dashboards without giving up control" in response.content
    assert b"Dashboard code runs outside the trusted portal" in response.content
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert (
        "frame-src http://content.agorausercontent.test:8001"
        in response.headers["Content-Security-Policy"]
    )


@pytest.mark.smoke
def test_portal_rejects_the_content_hostname(client: Client) -> None:
    response = client.get("/", HTTP_HOST="content.agorausercontent.test")

    assert response.status_code == 400


@pytest.mark.smoke
@override_settings(
    ROOT_URLCONF="agora.urls.content",
    ALLOWED_HOSTS=["content.agorausercontent.test"],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "agora.middleware.ContentSecurityHeadersMiddleware",
    ],
)
def test_content_service_starts_fail_closed(client: Client) -> None:
    for path in ("/", "/arbitrary/path", "/artifact/1/dashboard.html", "/csv/report.csv"):
        response = client.get(path, HTTP_HOST="content.agorausercontent.test")

        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts" in response.headers["Content-Security-Policy"]
        assert (
            "frame-ancestors http://portal.agora.test:8000"
            in response.headers["Content-Security-Policy"]
        )

    assert (
        client.post(
            "/artifact/1/dashboard.html", HTTP_HOST="content.agorausercontent.test"
        ).status_code
        == 404
    )


@pytest.mark.smoke
def test_service_entrypoints_and_content_composition_import() -> None:
    for module_name in (
        "agora.asgi",
        "agora.wsgi",
        "agora.content_asgi",
        "agora.content_wsgi",
    ):
        module = cast(ServiceEntrypoint, import_module(module_name))
        assert callable(module.application)

    content_settings = cast(ContentComposition, import_module("agora.settings.content"))
    assert content_settings.ALLOWED_HOSTS == ["content.agorausercontent.test"]
    assert content_settings.INSTALLED_APPS == ["agora.core.apps.CoreConfig"]
    assert content_settings.SECRET_KEY != settings.SECRET_KEY
    assert all("session" not in middleware.lower() for middleware in content_settings.MIDDLEWARE)


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
