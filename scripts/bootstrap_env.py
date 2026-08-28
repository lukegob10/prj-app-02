"""Create a non-committed local .env with generated development secrets."""

from __future__ import annotations

import os
import secrets
from getpass import getpass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / ".env.example"
OUTPUT_PATH = PROJECT_ROOT / ".env"


def main() -> int:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    artifact_root = (PROJECT_ROOT / ".local" / "artifacts").as_posix()
    secret_values = iter(
        (
            secrets.token_urlsafe(64),
            secrets.token_urlsafe(64),
        )
    )
    oracle_password = os.environ.get("TA_PROD_PASSWORD") or getpass(
        "Oracle password for the local PROD profile: "
    )
    if not oracle_password:
        print("Oracle password cannot be blank.")
        return 1
    lines: list[str] = []
    for line in template.splitlines():
        if line == "AGORA_PORTAL_SECRET_KEY=GENERATE_WITH_BOOTSTRAP":
            line = f"AGORA_PORTAL_SECRET_KEY={next(secret_values)}"
        elif line == "AGORA_CONTENT_SECRET_KEY=GENERATE_WITH_BOOTSTRAP":
            line = f"AGORA_CONTENT_SECRET_KEY={next(secret_values)}"
        elif line == "TA_PROD_PASSWORD=SET_LOCALLY":
            line = f"TA_PROD_PASSWORD={oracle_password}"
        elif line == "AGORA_ARTIFACT_ROOT=SET_BY_BOOTSTRAP":
            line = f"AGORA_ARTIFACT_ROOT={artifact_root}"
        lines.append(line)

    try:
        with OUTPUT_PATH.open("x", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
    except FileExistsError:
        print(".env already exists; refusing to overwrite it.")
        return 1

    if os.name != "nt":
        OUTPUT_PATH.chmod(0o600)
    print("Created .env with generated development-only secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
