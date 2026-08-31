from __future__ import annotations

import os
from pathlib import Path

import pytest
from django.test import Client, override_settings

import agora.health as health

PORTAL_HOST = "portal.agora.test"
CONTENT_HOST = "content.agorausercontent.test"


def test_database_probe_accepts_usable_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class HealthyConnection:
        def ensure_connection(self) -> None:
            pass

        def is_usable(self) -> bool:
            return True

    monkeypatch.setattr(health, "connection", HealthyConnection())

    assert health._database_is_usable() is True


def test_database_probe_collapses_dependency_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenConnection:
        def ensure_connection(self) -> None:
            raise RuntimeError("connection detail must stay private")

        def is_usable(self) -> bool:
            raise AssertionError("is_usable must not run after connect failure")

    monkeypatch.setattr(health, "connection", BrokenConnection())

    assert health._database_is_usable() is False


def test_database_probe_fails_closed_when_another_probe_is_running() -> None:
    with health._DATABASE_PROBE_LOCK:
        assert health._database_is_usable() is False


def test_private_volume_probe_checks_directory_type_and_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with override_settings(AGORA_ARTIFACT_ROOT=missing):
        assert health._artifact_root_is_usable(os.R_OK | os.X_OK) is False

    root = tmp_path / "private"
    root.mkdir()
    with override_settings(AGORA_ARTIFACT_ROOT=root):
        assert health._artifact_root_is_usable(os.R_OK | os.X_OK) is True
        monkeypatch.setattr(os, "access", lambda path, access: False)
        assert health._artifact_root_is_usable(os.R_OK | os.X_OK) is False

    file_path = tmp_path / "not-a-directory"
    file_path.write_bytes(b"not an artifact response")
    with override_settings(AGORA_ARTIFACT_ROOT=file_path):
        assert health._artifact_root_is_usable(os.R_OK | os.X_OK) is False

    with override_settings(AGORA_ARTIFACT_ROOT=None):
        assert health._artifact_root_is_usable(os.R_OK | os.X_OK) is False


@pytest.mark.smoke
def test_portal_liveness_is_dependency_free(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe() -> bool:
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr(health, "_database_is_usable", unexpected_probe)
    monkeypatch.setattr(health, "_artifact_root_is_usable", unexpected_probe)

    response = client.get("/health/live/", HTTP_HOST=PORTAL_HOST)

    assert response.status_code == 200
    assert response.content == b"ok\n"
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert client.get("/health/live/extra/", HTTP_HOST=PORTAL_HOST).status_code == 404


@pytest.mark.smoke
def test_portal_readiness_checks_metadata_and_private_volume(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probes: list[int] = []
    monkeypatch.setattr(health, "_database_is_usable", lambda: True)

    def usable_artifact(access: int) -> bool:
        probes.append(access)
        return True

    monkeypatch.setattr(
        health,
        "_artifact_root_is_usable",
        usable_artifact,
    )
    with override_settings(AGORA_ARTIFACT_ROOT=tmp_path):
        response = client.get("/health/ready/", HTTP_HOST=PORTAL_HOST)

    assert response.status_code == 200
    assert response.content == b"ready\n"
    assert probes


@pytest.mark.smoke
def test_portal_readiness_is_generic_and_fails_closed(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(health, "_database_is_usable", lambda: False)
    secret_path = tmp_path / "should-not-be-disclosed"
    with override_settings(AGORA_ARTIFACT_ROOT=secret_path):
        response = client.get("/health/ready/", HTTP_HOST=PORTAL_HOST)

    assert response.status_code == 503
    assert response.content == b"not ready\n"
    assert str(secret_path).encode() not in response.content


@pytest.mark.smoke
def test_portal_readiness_fails_when_private_storage_is_unavailable(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "_database_is_usable", lambda: True)
    monkeypatch.setattr(health, "_artifact_root_is_usable", lambda access: False)

    response = client.get("/health/ready/", HTTP_HOST=PORTAL_HOST)

    assert response.status_code == 503
    assert response.content == b"not ready\n"


@pytest.mark.smoke
@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_REDIRECT_EXEMPT=[r"^health/(?:live|ready)/$"],
)
def test_http_health_is_the_only_ssl_redirect_exception(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "_database_is_usable", lambda: True)
    monkeypatch.setattr(health, "_artifact_root_is_usable", lambda access: True)

    assert client.get("/health/live/", HTTP_HOST=PORTAL_HOST).status_code == 200
    assert client.get("/health/ready/", HTTP_HOST=PORTAL_HOST).status_code == 200
    portal = client.get("/", HTTP_HOST=PORTAL_HOST)
    assert portal.status_code == 301
    assert portal["Location"] == "https://portal.agora.test/"


@pytest.mark.smoke
@override_settings(
    ROOT_URLCONF="agora.urls.content",
    ALLOWED_HOSTS=[CONTENT_HOST],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "agora.middleware.ContentSecurityHeadersMiddleware",
    ],
)
def test_content_health_routes_preserve_the_isolated_policy(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hostile = b"<script>window.parent.document.body.innerHTML = 'owned'</script>"
    (tmp_path / "hostile.html").write_bytes(hostile)
    monkeypatch.setattr(health, "_database_is_usable", lambda: True)
    monkeypatch.setattr(health, "_artifact_root_is_usable", lambda access: True)

    live = client.get("/health/live/", HTTP_HOST=CONTENT_HOST)
    with override_settings(AGORA_ARTIFACT_ROOT=tmp_path):
        ready = client.get("/health/ready/", HTTP_HOST=CONTENT_HOST)
    unknown = client.get("/health/live/hostile.html", HTTP_HOST=CONTENT_HOST)
    ready_extra = client.get("/health/ready/extra/", HTTP_HOST=CONTENT_HOST)

    assert live.status_code == 200
    assert live.content == b"ok\n"
    assert ready.status_code == 200
    assert ready.content == b"ready\n"
    assert unknown.status_code == 404
    assert ready_extra.status_code == 404
    assert hostile not in live.content + ready.content + unknown.content
    assert live["Cache-Control"] == "private, no-store"
    assert ready["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in live["Content-Security-Policy"]
    assert "sandbox allow-scripts" in ready["Content-Security-Policy"]


@pytest.mark.parametrize("path", ["/health/live/", "/health/ready/"])
def test_portal_health_routes_allow_only_probe_methods(client: Client, path: str) -> None:
    response = client.post(path, HTTP_HOST=PORTAL_HOST)

    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"


@pytest.mark.parametrize("path", ["/health/live/", "/health/ready/"])
@override_settings(
    ROOT_URLCONF="agora.urls.content",
    ALLOWED_HOSTS=[CONTENT_HOST],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "agora.middleware.ContentSecurityHeadersMiddleware",
    ],
)
def test_content_health_routes_allow_only_probe_methods(client: Client, path: str) -> None:
    response = client.post(path, HTTP_HOST=CONTENT_HOST)

    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"
