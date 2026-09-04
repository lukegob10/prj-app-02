from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.conf import settings
from django.test import Client

from agora.portal import views as portal_views
from agora.rendering.security import portal_response_headers


def test_portal_referrer_policy_preserves_same_origin_form_provenance() -> None:
    assert settings.SECURE_REFERRER_POLICY == "same-origin"
    assert portal_response_headers(settings.AGORA_CONTENT_ORIGIN)["Referrer-Policy"] == (
        "same-origin"
    )


@pytest.mark.parametrize(
    ("origin", "include_token", "expected_status"),
    [
        ("http://localhost:8000", True, 200),
        ("http://localhost:8000", False, 403),
        ("null", True, 403),
        ("http://attacker.example", True, 403),
        ("http://127.0.0.1:8001", True, 403),
        ("http://localhost:9999", True, 403),
    ],
)
def test_login_still_requires_a_csrf_token_and_trusted_origin(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    include_token: bool,
    expected_status: int,
) -> None:
    authenticate = Mock(return_value=None)
    monkeypatch.setattr(portal_views, "authenticate_login", authenticate)
    client = Client(enforce_csrf_checks=True)
    response = client.get("/login/", HTTP_HOST="localhost:8000")
    assert response.status_code == 200
    data = {"soeid": "", "password": ""}
    if include_token:
        data["csrfmiddlewaretoken"] = client.cookies[settings.CSRF_COOKIE_NAME].value

    response = client.post(
        "/login/",
        data,
        HTTP_HOST="localhost:8000",
        HTTP_ORIGIN=origin,
    )

    assert response.status_code == expected_status
    assert authenticate.call_count == (1 if expected_status == 200 else 0)
