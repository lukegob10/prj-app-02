"""Start both local HTTPS entry points and stop them together."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNERS = ("run_https.py", "run_content_https.py")


def main() -> None:
    """Run portal and isolated content processes as one local developer command."""
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for runner in RUNNERS:
            processes.append(
                subprocess.Popen(
                    [sys.executable, str(PROJECT_ROOT / "scripts" / runner)],
                    cwd=PROJECT_ROOT,
                )
            )
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next((process.returncode for process in processes if process.returncode), 0)
        raise SystemExit(failed)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
