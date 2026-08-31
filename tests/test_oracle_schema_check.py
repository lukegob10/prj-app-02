"""Contract coverage for the offline Oracle schema artifact gate."""

from __future__ import annotations

import subprocess
import sys

import pytest

from scripts import check as quality_gate

_SCHEMA_CHECK_COMMAND = (
    sys.executable,
    "scripts/generate_oracle_schema.py",
    "--check",
)


def test_quality_gate_uses_the_fixed_offline_schema_check_command() -> None:
    schema_checks = [
        check for check in quality_gate.CHECKS if check.label == "Oracle schema artifact"
    ]

    assert len(schema_checks) == 1
    assert schema_checks[0].command == _SCHEMA_CHECK_COMMAND


def test_quality_gate_runs_schema_check_before_oracle_migration_apply() -> None:
    labels = [check.label for check in quality_gate.CHECKS]

    assert labels.index("Oracle schema artifact") < labels.index("migration apply")


def test_quality_gate_propagates_schema_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...], *, check: bool, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert set(kwargs) <= {"cwd"}
        commands.append(command)
        return subprocess.CompletedProcess(command, 1 if command == _SCHEMA_CHECK_COMMAND else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert quality_gate.main() == 1
    assert commands[-1] == _SCHEMA_CHECK_COMMAND
