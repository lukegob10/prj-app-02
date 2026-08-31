"""ASGI safeguards for the trusted reverse-proxy boundary."""

from __future__ import annotations

from typing import cast

from asgiref.typing import (
    ASGI3Application,
    ASGIReceiveCallable,
    ASGISendCallable,
    Scope,
)


class ForwardedProtoSanitizer:
    """Remove the raw scheme header after Uvicorn applies its peer allowlist.

    Uvicorn's outer proxy middleware converts a trusted peer's forwarded scheme
    into the ASGI scope. It does not remove the original header, however. Django
    must therefore see only the normalized scope scheme, never an untrusted raw
    ``X-Forwarded-Proto`` value.
    """

    def __init__(self, application: ASGI3Application) -> None:
        self.application = application

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = cast(
                Scope,
                {
                    **scope,
                    "headers": [
                        (name, value)
                        for name, value in scope["headers"]
                        if name.lower() != b"x-forwarded-proto"
                    ],
                },
            )
        await self.application(scope, receive, send)


__all__ = ["ForwardedProtoSanitizer"]
