"""Run the canonical local/CI quality gate without shell-specific behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_RESET_ALLOWED_ENV: Final = "AGORA_TEST_DATABASE_RESET_ALLOWED"
DATABASE_PREFLIGHT_FAILURE_EXIT_CODE: Final = 2


def database_reset_is_explicitly_allowed(environ: Mapping[str, str]) -> bool:
    """Require both raw process opt-ins before the canonical gate can touch Oracle."""
    return (
        environ.get("AGORA_ENVIRONMENT") == "test"
        and environ.get(TEST_DATABASE_RESET_ALLOWED_ENV) == "true"
    )


def database_acknowledgement_error() -> str:
    """Describe the destructive-gate acknowledgement without exposing configuration values."""
    return (
        "AGORA_ENVIRONMENT=test and "
        f"{TEST_DATABASE_RESET_ALLOWED_ENV}=true are required before database checks. "
        "Set them only when the selected Oracle schema is disposable and dedicated to "
        "Agora validation."
    )


@dataclass(frozen=True, slots=True)
class Check:
    label: str
    command: tuple[str, ...]


CHECKS = (
    Check("dependency lock", ("uv", "lock", "--check")),
    Check("format", ("ruff", "format", "--check", ".")),
    Check("lint", ("ruff", "check", ".")),
    Check("types", ("mypy", "src", "tests", "scripts", "deploy")),
    Check(
        "bytecode compile",
        (sys.executable, "-m", "compileall", "-q", "src", "scripts", "deploy"),
    ),
    Check("portal system checks", (sys.executable, "manage.py", "check", "--database", "default")),
    Check(
        "content system checks",
        (
            sys.executable,
            "-m",
            "django",
            "check",
            "--database",
            "default",
            "--settings=agora.settings.content",
        ),
    ),
    Check(
        "static asset collection",
        (sys.executable, "manage.py", "collectstatic", "--noinput"),
    ),
    Check(
        "migration drift",
        (sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"),
    ),
    Check(
        "Oracle schema artifact",
        (sys.executable, "scripts/generate_oracle_schema.py", "--check"),
    ),
    Check("migration apply", (sys.executable, "manage.py", "migrate", "--noinput")),
    Check("tests", ("pytest", "--browser", "chromium")),
    Check("package build", ("uv", "build")),
)


def main() -> int:
    if not database_reset_is_explicitly_allowed(os.environ):
        print(database_acknowledgement_error(), file=sys.stderr)
        return DATABASE_PREFLIGHT_FAILURE_EXIT_CODE

    for check in CHECKS:
        print(f"\n==> {check.label}", flush=True)
        completed = subprocess.run(check.command, check=False, cwd=PROJECT_ROOT)
        if completed.returncode:
            print(f"FAILED: {check.label}", file=sys.stderr)
            return completed.returncode
    print("\nAll quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
