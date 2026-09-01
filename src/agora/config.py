"""Typed, redacted runtime configuration validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import SplitResult, urlsplit

from django.core.exceptions import ImproperlyConfigured

EnvironmentName = Literal["development", "test", "production"]
ServiceName = Literal["portal", "content"]
_ENVIRONMENTS: frozenset[str] = frozenset({"development", "test", "production"})
_SERVICE_SECRET_NAMES: dict[ServiceName, str] = {
    "portal": "AGORA_PORTAL_SECRET_KEY",
    "content": "AGORA_CONTENT_SECRET_KEY",
}
_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "CHANGEME",
        "GENERATE_LOCALLY",
        "GENERATE_WITH_BOOTSTRAP",
        "SET_BY_BOOTSTRAP",
        "SET_LOCALLY",
        "SET_TO_ABSOLUTE_PATH",
    }
)


@dataclass(frozen=True, slots=True)
class Origin:
    """A validated HTTP origin with no path, query, fragment, or credentials."""

    value: str
    scheme: str
    hostname: str
    port: int | None


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Treasury Analytics Oracle profile selection."""

    environment: str

    @property
    def password_variable(self) -> str:
        """Return the credential name consumed by the local package."""

        return f"TA_{self.environment}_PASSWORD"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """All configuration required by either Agora service entry point."""

    environment: EnvironmentName
    debug: bool
    portal_origin: Origin
    content_origin: Origin
    database: DatabaseConfig
    artifact_root: Path

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> RuntimeConfig:
        errors: list[str] = []

        environment_value = _required(environ, "AGORA_ENVIRONMENT", errors)
        environment: EnvironmentName = "development"
        if environment_value and environment_value not in _ENVIRONMENTS:
            errors.append("AGORA_ENVIRONMENT must be development, test, or production")
        elif environment_value:
            environment = cast(EnvironmentName, environment_value)

        debug = _parse_bool(environ, "AGORA_DEBUG", errors)
        portal_origin = _parse_origin(environ, "AGORA_PORTAL_ORIGIN", errors)
        content_origin = _parse_origin(environ, "AGORA_CONTENT_ORIGIN", errors)
        if portal_origin and content_origin and portal_origin.hostname == content_origin.hostname:
            errors.append("portal and content origins must use different hostnames")

        if environment == "production":
            if debug:
                errors.append("AGORA_DEBUG must be false in production")
            for name, origin in (
                ("AGORA_PORTAL_ORIGIN", portal_origin),
                ("AGORA_CONTENT_ORIGIN", content_origin),
            ):
                if origin and origin.scheme != "https":
                    errors.append(f"{name} must use https in production")

        database = _parse_database(environ, errors)
        artifact_root = _parse_absolute_path(environ, "AGORA_ARTIFACT_ROOT", errors)

        if errors:
            detail = "\n".join(f"- {error}" for error in errors)
            raise ImproperlyConfigured(f"Agora configuration is invalid:\n{detail}")

        assert portal_origin is not None
        assert content_origin is not None
        assert database is not None
        assert artifact_root is not None
        return cls(
            environment=environment,
            debug=debug,
            portal_origin=portal_origin,
            content_origin=content_origin,
            database=database,
            artifact_root=artifact_root,
        )


def load_service_secret(environ: Mapping[str, str], service: ServiceName) -> str:
    """Load only the Django secret owned by the selected service composition."""
    errors: list[str] = []
    name = _SERVICE_SECRET_NAMES[service]
    value = _parse_secret(environ, name, errors)
    sibling_service: ServiceName = "content" if service == "portal" else "portal"
    sibling_value = environ.get(_SERVICE_SECRET_NAMES[sibling_service], "").strip()
    if value and sibling_value and value == sibling_value:
        errors.append("portal and content secret keys must be different")
    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        raise ImproperlyConfigured(f"Agora configuration is invalid:\n{detail}")
    return value


def load_portal_static_root(
    environ: Mapping[str, str],
    *,
    environment: EnvironmentName,
    development_default: Path,
) -> Path:
    """Resolve the portal static output without relying on an installed-wheel path."""

    value = environ.get("AGORA_STATIC_ROOT", "").strip()
    if not value and environment != "production":
        return development_default.resolve(strict=False)

    errors: list[str] = []
    static_root = _parse_absolute_path(environ, "AGORA_STATIC_ROOT", errors)
    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        raise ImproperlyConfigured(f"Agora configuration is invalid:\n{detail}")
    assert static_root is not None
    return static_root


def validate_treasury_package(
    environment: EnvironmentName,
    *,
    development_stand_in: bool,
    distribution_present: bool,
    distribution_summary: str,
) -> None:
    """Reject a development-only Oracle package in production."""

    normalized_summary = distribution_summary.casefold()
    local_summary = "stand-in" in normalized_summary or "development-only" in normalized_summary
    if environment == "production" and (
        development_stand_in or local_summary or not distribution_present
    ):
        raise ImproperlyConfigured(
            "Agora production requires the managed treasury-analytics package; "
            "the repository development stand-in is not allowed."
        )


def _required(
    environ: Mapping[str, str],
    name: str,
    errors: list[str],
    *,
    reject_placeholder: bool = False,
) -> str:
    value = environ.get(name, "")
    if not value.strip():
        errors.append(f"{name} is required")
        return ""
    if reject_placeholder and value.strip().upper() in _PLACEHOLDERS:
        errors.append(f"{name} still contains a placeholder")
        return ""
    return value


def _parse_bool(environ: Mapping[str, str], name: str, errors: list[str]) -> bool:
    value = _required(environ, name, errors).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    if value:
        errors.append(f"{name} must be true or false")
    return False


def _parse_secret(environ: Mapping[str, str], name: str, errors: list[str]) -> str:
    value = _required(environ, name, errors, reject_placeholder=True)
    if value and len(value) < 50:
        errors.append(f"{name} must contain at least 50 characters")
    return value


def _parse_origin(environ: Mapping[str, str], name: str, errors: list[str]) -> Origin | None:
    value = _required(environ, name, errors).strip()
    if not value:
        return None
    if any(character.isspace() for character in value):
        errors.append(f"{name} must not contain whitespace")
        return None

    try:
        parts: SplitResult = urlsplit(value)
        port = parts.port
    except ValueError:
        errors.append(f"{name} must be a valid HTTP origin")
        return None

    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        errors.append(f"{name} must contain only scheme, hostname, and optional port")
        return None

    normalized = f"{parts.scheme}://{parts.hostname.lower()}"
    if port is not None:
        normalized = f"{normalized}:{port}"
    if value.lower() != normalized:
        errors.append(f"{name} must be a normalized origin without a trailing slash")
        return None

    return Origin(
        value=normalized,
        scheme=parts.scheme,
        hostname=parts.hostname.lower(),
        port=port,
    )


def _parse_database(environ: Mapping[str, str], errors: list[str]) -> DatabaseConfig | None:
    value = _required(environ, "ENV", errors).strip().upper()
    if not value:
        return None
    if re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", value) is None:
        errors.append("ENV must begin with a letter and contain only A-Z, 0-9, _ or -")
        return None
    _required(environ, f"TA_{value}_PASSWORD", errors, reject_placeholder=True)
    return DatabaseConfig(environment=value)


def _parse_absolute_path(environ: Mapping[str, str], name: str, errors: list[str]) -> Path | None:
    value = _required(environ, name, errors, reject_placeholder=True).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        errors.append(f"{name} must be an absolute path")
        return None
    return path.resolve(strict=False)
