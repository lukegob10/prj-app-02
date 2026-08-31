"""Probe the two deployed Agora origins without sending credentials."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_HEALTH_CHECKS = (
    ("liveness", "/health/live/", b"ok\n"),
    ("readiness", "/health/ready/", b"ready\n"),
)
_PORTAL_STATIC_PATH = "/static/portal/foundation.css"
_MAX_RESPONSE_BYTES = 64


@dataclass(frozen=True, slots=True)
class ProbeFailure:
    """A safe, operator-facing probe failure with no response-body detail."""

    origin_label: str
    check_label: str
    detail: str

    def format(self) -> str:
        return f"{self.origin_label} {self.check_label}: {self.detail}"


class _RejectRedirects(HTTPRedirectHandler):
    """Treat a health redirect as a failed deployment contract."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def validate_origin(value: str, *, option: str) -> str:
    """Validate one canonical HTTPS origin before constructing probe URLs."""
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{option} must be a non-empty HTTPS origin")
    try:
        parts = urlsplit(value)
        _ = parts.port
        normalized = _serialize_origin(parts)
    except ValueError as error:
        raise ValueError(f"{option} must be a valid HTTPS origin") from error
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or value != normalized
    ):
        raise ValueError(
            f"{option} must contain only a normalized HTTPS scheme, hostname, and port"
        )
    return normalized


def probe_origin(
    origin: str,
    *,
    origin_label: str,
    timeout: float,
    opener: object | None = None,
) -> list[ProbeFailure]:
    """Probe exact health paths and return safe failures instead of raising network details."""
    active_opener = opener or build_opener(_RejectRedirects())
    failures: list[ProbeFailure] = []
    for check_label, path, expected_body in _HEALTH_CHECKS:
        request = Request(
            f"{origin}{path}",
            headers={"Accept": "text/plain"},
            method="GET",
        )
        try:
            with active_opener.open(request, timeout=timeout) as response:  # type: ignore[attr-defined]
                status = response.getcode()
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            failures.append(ProbeFailure(origin_label, check_label, f"HTTP {error.code}"))
            continue
        except (OSError, URLError) as error:
            failures.append(ProbeFailure(origin_label, check_label, type(error).__name__))
            continue
        if status != 200:
            failures.append(ProbeFailure(origin_label, check_label, f"HTTP {status}"))
        elif body != expected_body:
            failures.append(ProbeFailure(origin_label, check_label, "unexpected response"))
    return failures


def probe_portal_static(
    origin: str,
    *,
    timeout: float,
    opener: object | None = None,
) -> list[ProbeFailure]:
    """Prove the release's collected stylesheet is served by the trusted portal origin."""

    active_opener = opener or build_opener(_RejectRedirects())
    request = Request(
        f"{origin}{_PORTAL_STATIC_PATH}",
        headers={"Accept": "text/css"},
        method="GET",
    )
    try:
        with active_opener.open(request, timeout=timeout) as response:  # type: ignore[attr-defined]
            status = response.getcode()
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip()
    except HTTPError as error:
        return [ProbeFailure("portal", "static", f"HTTP {error.code}")]
    except (OSError, URLError) as error:
        return [ProbeFailure("portal", "static", type(error).__name__)]
    if status != 200:
        return [ProbeFailure("portal", "static", f"HTTP {status}")]
    if content_type.lower() != "text/css" or not body:
        return [ProbeFailure("portal", "static", "unexpected response")]
    return []


def run(*, portal_origin: str, content_origin: str, timeout: float) -> int:
    """Validate both origins, health, and the release's portal static output."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than zero")
    portal = validate_origin(portal_origin, option="--portal-origin")
    content = validate_origin(content_origin, option="--content-origin")
    if urlsplit(portal).hostname == urlsplit(content).hostname:
        raise ValueError("portal and content origins must use different hostnames")
    failures = [
        *probe_origin(portal, origin_label="portal", timeout=timeout),
        *probe_portal_static(portal, timeout=timeout),
        *probe_origin(content, origin_label="content", timeout=timeout),
    ]
    for failure in failures:
        print(failure.format(), file=sys.stderr)
    return 1 if failures else 0


def _serialize_origin(parts: SplitResult) -> str:
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("origin must use HTTP(S) and include a hostname")
    hostname = parts.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    origin = f"{parts.scheme}://{hostname}"
    if parts.port is not None:
        origin = f"{origin}:{parts.port}"
    return origin


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-origin", required=True)
    parser.add_argument("--content-origin", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(
            portal_origin=args.portal_origin,
            content_origin=args.content_origin,
            timeout=args.timeout,
        )
    except ValueError as error:
        _parser().error(str(error))
    if result == 0:
        print("Agora deployment smoke checks passed.")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
