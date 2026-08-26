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


class InvalidSoeid(ValueError):
    """Raised when an identity value cannot be made into a canonical SOEID."""


class InvalidLogicalName(ValueError):
    """Raised when a logical artifact name is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class LogicalName:
    """A display name plus its platform-independent uniqueness key."""

    display: str
    comparison_key: str


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
