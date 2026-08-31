from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import check as check_script


@dataclass(frozen=True, slots=True)
class _Completed:
    returncode: int


def _acknowledge_disposable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGORA_ENVIRONMENT", "test")
    monkeypatch.setenv(check_script.TEST_DATABASE_RESET_ALLOWED_ENV, "true")


def test_main_runs_all_checks_from_repository_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[str, ...], bool, Path]] = []

    def run(command: tuple[str, ...], *, check: bool, cwd: Path) -> _Completed:
        calls.append((command, check, cwd))
        return _Completed(returncode=0)

    monkeypatch.chdir(tmp_path)
    _acknowledge_disposable_database(monkeypatch)
    monkeypatch.setattr(subprocess, "run", run)

    assert check_script.main() == 0

    assert [command for command, _, _ in calls] == [check.command for check in check_script.CHECKS]
    assert all(check is False for _, check, _ in calls)
    assert all(cwd == check_script.PROJECT_ROOT for _, _, cwd in calls)


def test_main_stops_at_the_first_failed_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    failure_index = 2
    failure_code = 17

    def run(command: tuple[str, ...], *, check: bool, cwd: Path) -> _Completed:
        del check, cwd
        calls.append(command)
        return _Completed(returncode=failure_code if len(calls) == failure_index else 0)

    monkeypatch.chdir(tmp_path)
    _acknowledge_disposable_database(monkeypatch)
    monkeypatch.setattr(subprocess, "run", run)

    assert check_script.main() == failure_code

    expected = [check.command for check in check_script.CHECKS[:failure_index]]
    assert calls == expected


@pytest.mark.parametrize(
    ("environment", "acknowledgement"),
    [
        (None, None),
        ("development", "true"),
        ("test", None),
        ("test", "false"),
        ("test", "TRUE"),
    ],
)
def test_main_rejects_unsafe_database_configuration_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: str | None,
    acknowledgement: str | None,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], *, check: bool, cwd: Path) -> _Completed:
        del check, cwd
        calls.append(command)
        return _Completed(returncode=0)

    if environment is None:
        monkeypatch.delenv("AGORA_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("AGORA_ENVIRONMENT", environment)
    if acknowledgement is None:
        monkeypatch.delenv(check_script.TEST_DATABASE_RESET_ALLOWED_ENV, raising=False)
    else:
        monkeypatch.setenv(check_script.TEST_DATABASE_RESET_ALLOWED_ENV, acknowledgement)
    monkeypatch.setattr(subprocess, "run", run)

    assert check_script.main() == check_script.DATABASE_PREFLIGHT_FAILURE_EXIT_CODE
    assert calls == []
    assert check_script.database_acknowledgement_error() in capsys.readouterr().err
