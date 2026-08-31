"""Canonical identity and logical artifact-name normalization."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SOEID_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}", flags=re.ASCII)
_ASCII_WHITESPACE = " \t\r\n\f\v"
_RESERVED_WINDOWS_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_MAX_LOGICAL_NAME_CODEPOINTS = 255
_MAX_NAME_KEY_BYTES = 1024
_MAX_DASHBOARD_TAG_CODEPOINTS = 40
_MAX_DASHBOARD_TAG_KEY_CODEPOINTS = 80


class InvalidSoeid(ValueError):
    """Raised when an identity value cannot be made into a canonical SOEID."""


class InvalidLogicalName(ValueError):
    """Raised when a logical artifact name is unsafe or ambiguous."""


class InvalidDashboardTag(ValueError):
    """Raised when a dashboard tag cannot be represented canonically."""


@dataclass(frozen=True, slots=True)
class LogicalName:
    """A display name plus its platform-independent uniqueness key."""

    display: str
    comparison_key: str


@dataclass(frozen=True, slots=True)
class DashboardTagName:
    """A short display label plus its case-insensitive uniqueness key."""

    display: str
    key: str


def canonicalize_soeid(value: str) -> str:
    """Normalize a trusted identity input using the product-contract grammar."""
    stripped = value.strip(_ASCII_WHITESPACE)
    if not stripped.isascii():
        raise InvalidSoeid("SOEID must contain ASCII characters only")
    canonical = stripped.upper()
    if SOEID_PATTERN.fullmatch(canonical) is None:
        raise InvalidSoeid("SOEID does not match the required canonical grammar")
    return canonical


def normalize_logical_name(value: str) -> LogicalName:
    """Validate a filename-like label without ever turning it into a path."""
    display = unicodedata.normalize("NFC", value)
    if not display or len(display) > _MAX_LOGICAL_NAME_CODEPOINTS:
        raise InvalidLogicalName("logical name must contain 1 through 255 characters")
    compatible = unicodedata.normalize("NFKC", display)
    if display in {".", ".."} or display.endswith((" ", ".")):
        raise InvalidLogicalName("logical name has a reserved form")
    if compatible in {".", ".."} or compatible.endswith((" ", ".")):
        raise InvalidLogicalName("logical name has a reserved form")
    if any(character in {"/", "\\", ":"} for character in compatible):
        raise InvalidLogicalName("logical name cannot contain path separators or a colon")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in display):
        raise InvalidLogicalName("logical name cannot contain control characters")

    stem = compatible.split(".", maxsplit=1)[0].upper().rstrip(" .")
    if stem in _RESERVED_WINDOWS_STEMS:
        raise InvalidLogicalName("logical name uses a reserved platform name")

    comparison_key = unicodedata.normalize("NFKC", compatible.casefold())
    if len(comparison_key.encode("utf-8")) > _MAX_NAME_KEY_BYTES:
        raise InvalidLogicalName("logical name comparison key is too long")
    return LogicalName(display=display, comparison_key=comparison_key)


def normalize_dashboard_tag(value: str) -> DashboardTagName:
    """Return one whitespace-normalized, Unicode-stable dashboard tag.

    The display label deliberately remains human-readable.  The separately
    persisted key is NFKC/casefold normalized so uniqueness does not depend on
    presentation casing.  Database slot and uniqueness constraints remain the
    final concurrency boundary.
    """
    if not isinstance(value, str):
        raise InvalidDashboardTag("tag must be text")
    compatible = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in compatible):
        raise InvalidDashboardTag("tag cannot contain control characters")
    display = " ".join(compatible.split())
    if not display or len(display) > _MAX_DASHBOARD_TAG_CODEPOINTS:
        raise InvalidDashboardTag("tag must contain 1 through 40 characters")
    key = unicodedata.normalize("NFKC", display.casefold())
    if len(key) > _MAX_DASHBOARD_TAG_KEY_CODEPOINTS:
        raise InvalidDashboardTag("normalized tag key is too long")
    return DashboardTagName(display=display, key=key)
