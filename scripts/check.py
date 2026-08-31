"""Run the canonical local/CI quality gate without shell-specific behavior."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


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
    for check in CHECKS:
        print(f"\n==> {check.label}", flush=True)
        completed = subprocess.run(check.command, check=False)
        if completed.returncode:
            print(f"FAILED: {check.label}", file=sys.stderr)
            return completed.returncode
    print("\nAll quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
