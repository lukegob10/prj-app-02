from __future__ import annotations

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from agora.middleware import ContentSecurityHeadersMiddleware, PortalSecurityHeadersMiddleware
from agora.rendering.security import (
    CONTENT_IFRAME_SANDBOX,
    apply_content_response_policy,
    apply_portal_response_policy,
    content_response_headers,
    content_security_policy,
    portal_content_iframe_attributes,
    portal_content_security_policy,
    portal_response_headers,
)

PORTAL_ORIGIN = "http://portal.agora.test:8000"
CONTENT_ORIGIN = "http://content.agorausercontent.test:8001"


def test_content_policy_is_the_approved_default_deny_baseline() -> None:
    assert content_security_policy(PORTAL_ORIGIN) == "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "script-src 'unsafe-inline'",
            "style-src 'unsafe-inline'",
            "img-src data: blob:",
            "font-src data:",
            "media-src data: blob:",
            "connect-src 'self'",
            "frame-src 'none'",
            "worker-src 'none'",
            "manifest-src 'none'",
            "form-action 'none'",
            f"frame-ancestors {PORTAL_ORIGIN}",
            "sandbox allow-scripts",
        )
    )
    assert content_response_headers(PORTAL_ORIGIN) == {
        "Cache-Control": "private, no-store",
        "Permissions-Policy": (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), "
            "payment=(), usb=()"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-DNS-Prefetch-Control": "off",
        "Content-Security-Policy": content_security_policy(PORTAL_ORIGIN),
    }


def test_portal_policy_frames_only_the_exact_content_origin() -> None:
    policy = portal_content_security_policy(CONTENT_ORIGIN)

    assert policy == "; ".join(
        (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            "script-src 'none'",
            "style-src 'self'",
            "img-src 'self'",
            "connect-src 'self'",
            f"frame-src {CONTENT_ORIGIN}",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )
    assert portal_response_headers(CONTENT_ORIGIN)["X-Frame-Options"] == "DENY"


def test_response_application_overwrites_weak_headers_and_removes_content_cookies() -> None:
    response = HttpResponse("fixture")
    response["Cache-Control"] = "public, max-age=3600"
    response["Content-Security-Policy"] = "default-src *"
    response["X-Frame-Options"] = "DENY"
    response["Set-Cookie"] = "portal_session=should-not-survive"
    response.set_cookie("content_probe", "should-not-survive")

    result = apply_content_response_policy(response, portal_origin=PORTAL_ORIGIN)

    assert result is response
    assert result["Cache-Control"] == "private, no-store"
    assert result["Content-Security-Policy"] == content_security_policy(PORTAL_ORIGIN)
    assert "X-Frame-Options" not in result.headers
    assert "Set-Cookie" not in result.headers
    assert not result.cookies


def test_portal_response_application_keeps_cookie_capability_for_future_sessions() -> None:
    response = HttpResponse("portal")
    response["Cache-Control"] = "public"
    response["Content-Security-Policy"] = "default-src *"
    response.set_cookie("portal_probe", "kept")

    result = apply_portal_response_policy(response, content_origin=CONTENT_ORIGIN)

    assert result["Cache-Control"] == "no-store"
    assert result["Content-Security-Policy"] == portal_content_security_policy(CONTENT_ORIGIN)
    assert result["X-Frame-Options"] == "DENY"
    assert result.cookies["portal_probe"].value == "kept"


@override_settings(
    AGORA_PORTAL_ORIGIN=PORTAL_ORIGIN,
    AGORA_CONTENT_ORIGIN=CONTENT_ORIGIN,
)
def test_configured_middlewares_overwrite_view_supplied_policies() -> None:
    request = RequestFactory().get("/")

    def weak_response(_request: HttpRequest) -> HttpResponse:
        response = HttpResponse("weak")
        response["Content-Security-Policy"] = "default-src *"
        response["Cache-Control"] = "public"
        response["X-Frame-Options"] = "SAMEORIGIN"
        response.set_cookie("content_probe", "removed")
        return response

    portal_response = PortalSecurityHeadersMiddleware(weak_response)(request)
    content_response = ContentSecurityHeadersMiddleware(weak_response)(request)

    assert portal_response["Content-Security-Policy"] == portal_content_security_policy(
        settings.AGORA_CONTENT_ORIGIN
    )
    assert portal_response["X-Frame-Options"] == "DENY"
    assert content_response["Content-Security-Policy"] == content_security_policy(
        settings.AGORA_PORTAL_ORIGIN
    )
    assert "X-Frame-Options" not in content_response.headers
    assert not content_response.cookies


def test_iframe_attributes_reenable_scripts_only_and_pin_the_content_origin() -> None:
    content_url = f"{CONTENT_ORIGIN}/revision/opaque.html?preview=1"

    assert portal_content_iframe_attributes(content_url, content_origin=CONTENT_ORIGIN) == {
        "src": content_url,
        "sandbox": CONTENT_IFRAME_SANDBOX,
        "referrerpolicy": "no-referrer",
    }
    assert "allow-same-origin" not in CONTENT_IFRAME_SANDBOX


@pytest.mark.parametrize(
    "origin",
    [
        "",
        " ",
        "ftp://portal.agora.test",
        "http://portal.agora.test/",
        "http://user@portal.agora.test",
        "http://portal.agora.test:invalid",
        "http://portal.agora.test:08000",
    ],
)
def test_policy_rejects_untrusted_or_noncanonical_origins(origin: str) -> None:
    with pytest.raises(ValueError):
        content_security_policy(origin)


@pytest.mark.parametrize(
    "content_url",
    [
        "",
        " ",
        "not-a-url",
        "ftp://content.agorausercontent.test:8001/fixture.html",
        "http://other.agora.test:8001/fixture.html",
        "http://user@content.agorausercontent.test:8001/fixture.html",
        "http://content.agorausercontent.test:8001/fixture.html#fragment",
        "http://content.agorausercontent.test:8001/fixture.html#",
        "http://content.agorausercontent.test:invalid/fixture.html",
    ],
)
def test_iframe_attributes_reject_untrusted_content_urls(content_url: str) -> None:
    with pytest.raises(ValueError):
        portal_content_iframe_attributes(content_url, content_origin=CONTENT_ORIGIN)
