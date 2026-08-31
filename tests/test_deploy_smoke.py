from __future__ import annotations

from dataclasses import dataclass
from urllib.request import Request

import pytest

from scripts.smoke_deploy import probe_origin, probe_portal_static, run, validate_origin


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "portal.example.test/",
        "ftp://portal.example.test",
        "http://portal.example.test",
        "https://user:password@portal.example.test",
        "https://portal.example.test/path",
        "https://portal.example.test?token=secret",
        "https://portal.example.test#fragment",
    ],
)
def test_smoke_validator_rejects_non_origins(origin: str) -> None:
    with pytest.raises(ValueError):
        validate_origin(origin, option="--portal-origin")


def test_smoke_validator_rejects_shared_hostname() -> None:
    with pytest.raises(ValueError, match="different hostnames"):
        run(
            portal_origin="https://portal.example.test",
            content_origin="https://portal.example.test:8444",
            timeout=1,
        )


@dataclass
class FakeResponse:
    body: bytes
    status: int = 200
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"Content-Type": "text/plain; charset=utf-8"}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self.body[:size]


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[Request] = []

    def open(self, request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 2
        self.requests.append(request)
        return next(self.responses)


def test_smoke_validator_checks_exact_paths_and_bodies() -> None:
    opener = FakeOpener([FakeResponse(b"ok\n"), FakeResponse(b"not ready\n", status=503)])

    failures = probe_origin(
        "https://portal.example.test",
        origin_label="portal",
        timeout=2,
        opener=opener,
    )

    assert [request.full_url for request in opener.requests] == [
        "https://portal.example.test/health/live/",
        "https://portal.example.test/health/ready/",
    ]
    assert all(request.get_header("Authorization") is None for request in opener.requests)
    assert [failure.format() for failure in failures] == ["portal readiness: HTTP 503"]


def test_smoke_validator_proves_portal_static_delivery() -> None:
    opener = FakeOpener(
        [
            FakeResponse(
                b":root { color-scheme: light; }",
                headers={"Content-Type": "text/css; charset=utf-8"},
            )
        ]
    )

    failures = probe_portal_static(
        "https://portal.example.test",
        timeout=2,
        opener=opener,
    )

    assert failures == []
    assert [request.full_url for request in opener.requests] == [
        "https://portal.example.test/static/portal/foundation.css"
    ]
    assert opener.requests[0].get_header("Authorization") is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_smoke_validator_rejects_invalid_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite number greater than zero"):
        run(
            portal_origin="https://portal.example.test",
            content_origin="https://content.example.test",
            timeout=timeout,
        )
