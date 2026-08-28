"""Non-secret connection coordinates owned by the local package stand-in."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

_PROFILES: Final = MappingProxyType(
    {
        "PROD": MappingProxyType(
            {
                "username": "LG22254",
                "hostname": "192.168.1.151",
                "port": 1521,
                "service_name": "FREEPDB1",
            }
        )
    }
)


def get_profile(environment: str) -> MappingProxyType[str, object]:
    """Return a configured profile or fail with the supported profile names."""

    normalized = (environment or "").strip().upper()
    if not normalized:
        raise ValueError("Connection environment cannot be blank")
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"Unknown connection environment {normalized!r}; supported environments: {supported}"
        ) from exc
