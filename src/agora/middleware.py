"""Response guardrails for the trusted and untrusted web entry points."""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

ResponseHandler = Callable[[HttpRequest], HttpResponse]

_COMMON_RESTRICTIVE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), "
        "payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-DNS-Prefetch-Control": "off",
}


class PortalSecurityHeadersMiddleware:
    """Keep portal documents trusted and frame only the configured content origin."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        for name, value in _COMMON_RESTRICTIVE_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'self'",
                    "base-uri 'none'",
                    "object-src 'none'",
                    "script-src 'none'",
                    "style-src 'self'",
                    "img-src 'self'",
                    "connect-src 'self'",
                    f"frame-src {settings.AGORA_CONTENT_ORIGIN}",
                    "form-action 'self'",
                    "frame-ancestors 'none'",
                )
            ),
        )
        return response


class ContentSecurityHeadersMiddleware:
    """Default-deny the future content surface until AG-007 adds authorized routes."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        for name, value in _COMMON_RESTRICTIVE_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'none'",
                    "base-uri 'none'",
                    "object-src 'none'",
                    "form-action 'none'",
                    f"frame-ancestors {settings.AGORA_PORTAL_ORIGIN}",
                    "sandbox",
                )
            ),
        )
        return response
