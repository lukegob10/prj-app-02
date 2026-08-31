"""Response guardrails for the trusted and untrusted web entry points."""

from collections.abc import Callable
from ipaddress import ip_address
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from agora.rendering.security import apply_content_response_policy, apply_portal_response_policy

ResponseHandler = Callable[[HttpRequest], HttpResponse]
UNSAFE_METHODS = frozenset({"DELETE", "PATCH", "POST", "PUT"})


class LoopbackOpaqueOriginMiddleware:
    """Normalize an opaque browser origin only for the explicit local HTTPS launcher."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._is_allowed_opaque_origin(request):
            request.META["HTTP_ORIGIN"] = settings.AGORA_PORTAL_ORIGIN
        return self.get_response(request)

    @staticmethod
    def _is_allowed_opaque_origin(request: HttpRequest) -> bool:
        if not getattr(settings, "AGORA_ALLOW_OPAQUE_LOOPBACK_ORIGIN", False):
            return False
        if settings.AGORA_ENVIRONMENT != "development":
            return False
        if request.method not in UNSAFE_METHODS or request.META.get("HTTP_ORIGIN") != "null":
            return False
        if not request.is_secure():
            return False

        portal = urlsplit(settings.AGORA_PORTAL_ORIGIN)
        if portal.scheme != "https" or request.get_host().lower() != portal.netloc.lower():
            return False

        remote_address = request.META.get("REMOTE_ADDR", "")
        try:
            return ip_address(remote_address).is_loopback
        except ValueError:
            return False


class PortalSecurityHeadersMiddleware:
    """Keep portal documents trusted and frame only the configured content origin."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        return apply_portal_response_policy(
            response,
            content_origin=settings.AGORA_CONTENT_ORIGIN,
            # Trusted portal behavior is shipped only as same-origin static modules. Inline and
            # third-party scripts remain blocked; hostile dashboard content uses a separate CSP.
            allow_scripts=True,
        )


class ContentSecurityHeadersMiddleware:
    """Apply the hostile-content policy to authorized responses and default-deny failures."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        return apply_content_response_policy(response, portal_origin=settings.AGORA_PORTAL_ORIGIN)
