from __future__ import annotations

from typing import cast

from asgiref.sync import async_to_sync
from asgiref.typing import (
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendCallable,
    ASGISendEvent,
    HTTPScope,
    LifespanScope,
    Scope,
)
from django.conf import settings
from django.test import Client, override_settings

from agora.proxy import ForwardedProtoSanitizer

PORTAL_HOST = "portal.agora.test"


async def _sanitized_scope() -> HTTPScope:
    captured: list[Scope] = []

    async def downstream(
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        del receive, send
        captured.append(scope)

    async def receive() -> ASGIReceiveEvent:
        return {"type": "http.disconnect"}

    async def send(event: ASGISendEvent) -> None:
        del event

    scope = cast(
        HTTPScope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"x-forwarded-proto", b"https"),
                (b"X-Forwarded-Proto", b"http"),
                (b"x-request-id", b"kept"),
            ],
            "client": ("198.51.100.25", 50000),
            "server": ("127.0.0.1", 8000),
        },
    )
    application = ForwardedProtoSanitizer(downstream)
    await application(scope, receive, send)
    return cast(HTTPScope, captured[0])


async def _passthrough_lifespan_scope() -> LifespanScope:
    captured: list[Scope] = []

    async def downstream(
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        del receive, send
        captured.append(scope)

    async def receive() -> ASGIReceiveEvent:
        return {"type": "lifespan.shutdown"}

    async def send(event: ASGISendEvent) -> None:
        del event

    scope = cast(
        LifespanScope,
        {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": {},
        },
    )
    application = ForwardedProtoSanitizer(downstream)
    await application(scope, receive, send)
    return cast(LifespanScope, captured[0])


def test_proxy_ssl_header_contract_is_explicit() -> None:
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


def test_raw_forwarded_proto_variants_are_removed_without_changing_scope_scheme() -> None:
    scope = async_to_sync(_sanitized_scope)()

    assert scope["scheme"] == "https"
    assert scope["headers"] == [(b"x-request-id", b"kept")]


def test_non_request_scope_passes_through_unchanged() -> None:
    scope = async_to_sync(_passthrough_lifespan_scope)()

    assert scope["type"] == "lifespan"
    assert scope["state"] == {}


@override_settings(SECURE_SSL_REDIRECT=True)
def test_normalized_https_scope_does_not_redirect_behind_the_proxy(client: Client) -> None:
    trusted_https = client.get("/login/", HTTP_HOST=PORTAL_HOST, secure=True)
    direct_http = client.get("/login/", HTTP_HOST=PORTAL_HOST)

    assert trusted_https.status_code == 200
    assert direct_http.status_code == 301
    assert direct_http["Location"] == "https://portal.agora.test/login/"
