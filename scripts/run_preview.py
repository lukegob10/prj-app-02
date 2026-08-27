"""Run the trusted portal on loopback with Django development autoreload."""

from __future__ import annotations

import os
import sys

from django.core.management import execute_from_command_line

PREVIEW_ORIGIN = "http://127.0.0.1:8002"


def main() -> None:
    """Start a development-only preview that needs no hosts-file entry."""
    os.environ["AGORA_PORTAL_ORIGIN"] = PREVIEW_ORIGIN
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.portal")
    execute_from_command_line(["manage.py", "runserver", "127.0.0.1:8002", *sys.argv[1:]])


if __name__ == "__main__":
    main()
