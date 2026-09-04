"""Fail-closed settings selection shared by Agora's process entry points."""

from __future__ import annotations

from collections.abc import MutableMapping

from django.core.exceptions import ImproperlyConfigured


def configure_django_settings(environ: MutableMapping[str, str], expected: str) -> None:
    """Select exactly one service composition and reject a poisoned inherited setting."""
    configured = environ.get("DJANGO_SETTINGS_MODULE")
    if configured is not None and configured != expected:
        raise ImproperlyConfigured(
            "DJANGO_SETTINGS_MODULE conflicts with the requested Agora service entry point"
        )
    environ["DJANGO_SETTINGS_MODULE"] = expected


__all__ = ["configure_django_settings"]
