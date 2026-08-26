from __future__ import annotations

import pytest

from agora.persistence.names import (
    InvalidLogicalName,
    InvalidSoeid,
    canonicalize_soeid,
    normalize_logical_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc123", "ABC123"),
        (" \tab.c-1_2\r\n", "AB.C-1_2"),
        ("A", "A"),
        ("A" * 64, "A" * 64),
    ],
)
def test_soeid_canonicalization_is_explicit(raw: str, expected: str) -> None:
    assert canonicalize_soeid(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "-ABC",
        "ABC+1",
        "A" * 65,
        "Straße",
        "\u0131",
        "\u017fOEID",
        "ABC\u00a0",
    ],
)
def test_soeid_rejects_invalid_and_non_ascii_inputs(raw: str) -> None:
    with pytest.raises(InvalidSoeid):
        canonicalize_soeid(raw)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("café.csv", "café.csv"),
        ("Report.csv", "report.CSV"),
        ("\uff2b.csv", "k.csv"),
        ("\u212a.csv", "k.csv"),
        ("Straße.csv", "STRASSE.csv"),
    ],
)
def test_logical_name_comparison_key_catches_unicode_and_case_collisions(
    first: str, second: str
) -> None:
    normalized_first = normalize_logical_name(first)
    normalized_second = normalize_logical_name(second)

    assert normalized_first.comparison_key == normalized_second.comparison_key


def test_logical_name_preserves_an_nfc_display_form() -> None:
    name = normalize_logical_name("café.csv")

    assert name.display == "café.csv"
    assert name.comparison_key == "café.csv"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".",
        "..",
        "../report.csv",
        "folder\\report.csv",
        "C:report.csv",
        "report.csv ",
        "report.csv.",
        "report\x00.csv",
        "report\n.csv",
        "report\u200b.csv",
        "CON",
        "nul.csv",
        "LPT9.txt",
        "x" * 256,
        ("ß" * 600) + ".csv",
    ],
)
def test_logical_name_rejects_path_control_reserved_and_oversized_forms(raw: str) -> None:
    with pytest.raises(InvalidLogicalName):
        normalize_logical_name(raw)
