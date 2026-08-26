"""Response guardrails for the trusted and untrusted web entry points."""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from agora.rendering.security import apply_content_response_policy, apply_portal_response_policy

ResponseHandler = Callable[[HttpRequest], HttpResponse]


class PortalSecurityHeadersMiddleware:
    """Keep portal documents trusted and frame only the configured content origin."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        return apply_portal_response_policy(response, content_origin=settings.AGORA_CONTENT_ORIGIN)


class ContentSecurityHeadersMiddleware:
    """Default-deny the future content surface until AG-007 adds authorized routes."""

    def __init__(self, get_response: ResponseHandler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        return apply_content_response_policy(response, portal_origin=settings.AGORA_PORTAL_ORIGIN)
