"""Browser security policy primitives shared by future renderer routes."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from django.http import HttpResponse

CONTENT_IFRAME_SANDBOX = "allow-scripts"
_NO_REFERRER = "no-referrer"
_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()"
)


def portal_content_security_policy(content_origin: str, *, allow_scripts: bool = False) -> str:
    """Return the exact policy for trusted portal documents."""
    origin = _validated_origin(content_origin)
    script_source = "'self'" if allow_scripts else "'none'"
    return _serialize_policy(
        (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            f"script-src {script_source}",
            "style-src 'self'",
            "img-src 'self'",
            "connect-src 'self'",
            f"frame-src {origin}",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


def content_security_policy(portal_origin: str) -> str:
    """Return the approved default-deny policy for hostile content responses."""
    origin = _validated_origin(portal_origin)
    return _serialize_policy(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "script-src 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "media-src data: blob:",
            "connect-src 'self'",
            "frame-src 'none'",
            "worker-src 'none'",
            "manifest-src 'none'",
            "form-action 'none'",
            f"frame-ancestors {origin}",
            f"sandbox {CONTENT_IFRAME_SANDBOX}",
        )
    )


def portal_response_headers(
    content_origin: str,
    *,
    allow_scripts: bool = False,
) -> dict[str, str]:
    """Return headers that protect the trusted portal and its frame boundary."""
    return {
        **_restrictive_headers(cache_control="no-store"),
        "Content-Security-Policy": portal_content_security_policy(
            content_origin,
            allow_scripts=allow_scripts,
        ),
        "X-Frame-Options": "DENY",
    }


def content_response_headers(portal_origin: str) -> dict[str, str]:
    """Return headers that every future content response must carry."""
    return {
        **_restrictive_headers(cache_control="private, no-store"),
        "Content-Security-Policy": content_security_policy(portal_origin),
    }


def apply_portal_response_policy(
    response: HttpResponse,
    *,
    content_origin: str,
    allow_scripts: bool = False,
) -> HttpResponse:
    """Enforce the portal policy after the view has produced its response."""
    for name, value in portal_response_headers(
        content_origin,
        allow_scripts=allow_scripts,
    ).items():
        response.headers[name] = value
    return response


def apply_content_response_policy(response: HttpResponse, *, portal_origin: str) -> HttpResponse:
    """Enforce content policy and remove response state that could create cookies."""
    response.cookies.clear()
    response.headers.pop("Set-Cookie", None)
    response.headers.pop("X-Frame-Options", None)
    for name, value in content_response_headers(portal_origin).items():
        response.headers[name] = value
    return response


def portal_content_iframe_attributes(content_url: str, *, content_origin: str) -> dict[str, str]:
    """Build the minimum safe iframe attributes for a content-origin navigation."""
    expected_origin = _validated_origin(content_origin)
    if not content_url or any(character.isspace() for character in content_url):
        raise ValueError("content URL must be a non-empty absolute URL without whitespace")

    try:
        parts = urlsplit(content_url)
        actual_origin = _serialized_origin(parts)
    except ValueError as error:
        raise ValueError("content URL must be a valid HTTP origin URL") from error

    if (
        actual_origin != expected_origin
        or parts.username is not None
        or parts.password is not None
        or "#" in content_url
    ):
        raise ValueError("content URL must use the configured content origin")

    return {
        "src": content_url,
        "sandbox": CONTENT_IFRAME_SANDBOX,
        "referrerpolicy": _NO_REFERRER,
    }


def _restrictive_headers(*, cache_control: str) -> dict[str, str]:
    return {
        "Cache-Control": cache_control,
        "Permissions-Policy": _PERMISSIONS_POLICY,
        "Referrer-Policy": _NO_REFERRER,
        "X-Content-Type-Options": "nosniff",
        "X-DNS-Prefetch-Control": "off",
    }


def _serialize_policy(directives: tuple[str, ...]) -> str:
    return "; ".join(directives)


def _validated_origin(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise ValueError("origin must be a non-empty HTTP origin without whitespace")
    try:
        parts = urlsplit(value)
        _ = parts.port
        normalized = _serialized_origin(parts)
    except ValueError as error:
        raise ValueError("origin must be a valid HTTP origin") from error

    if (
        parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or value != normalized
    ):
        raise ValueError("origin must contain only a normalized scheme, hostname, and port")
    return normalized


def _serialized_origin(parts: SplitResult) -> str:
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must use HTTP(S) and include a hostname")
    port = parts.port
    origin = f"{parts.scheme}://{parts.hostname.lower()}"
    if port is not None:
        origin = f"{origin}:{port}"
    return origin
