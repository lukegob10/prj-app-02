"""Single container entry point for the portal and isolated content services.

The service name is deliberately the first argument so a deployment can use one
image for both compositions without changing the Django entry-point contract.
Operational jobs run in the portal composition: it owns the complete migration
set and the collected static root.  Content serving never enables Uvicorn access
logging because its render bearer is part of the request path.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "portal": {
        "settings": "agora.settings.portal",
        "application": "agora.asgi:application",
    },
    "content": {
        "settings": "agora.settings.content",
        "application": "agora.content_asgi:application",
    },
}
COMMANDS = ("serve", "prepare")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
MAX_WORKERS = 32


def _port(value: str) -> int:
    """Parse a TCP port without allowing an invalid value to reach Uvicorn."""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _workers(value: str) -> int:
    """Bound worker fan-out so an accidental platform value cannot fork freely."""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("workers must be an integer") from error
    if not 1 <= parsed <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"workers must be between 1 and {MAX_WORKERS}")
    return parsed


def _forwarded_allow_ips(value: str) -> str:
    """Accept only explicit bounded IPs/networks; global trust is never allowed."""

    entries = [entry.strip() for entry in value.split(",")]
    if not entries or any(not entry for entry in entries):
        raise argparse.ArgumentTypeError("forwarded proxy allowlist cannot be empty")
    normalized: list[str] = []
    for entry in entries:
        if entry == "*":
            raise argparse.ArgumentTypeError("forwarded proxy allowlist must not contain '*'")
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
                if network.prefixlen == 0:
                    raise argparse.ArgumentTypeError(
                        "forwarded proxy allowlist must not trust the entire address space"
                    )
                normalized.append(str(network))
            else:
                normalized.append(str(ipaddress.ip_address(entry)))
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                "forwarded proxy allowlist must contain only IP addresses or CIDR networks"
            ) from error
    return ",".join(normalized)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=tuple(SERVICES))
    parser.add_argument("command", choices=COMMANDS, nargs="?", default="serve")
    parser.add_argument(
        "--host",
        default=os.environ.get("AGORA_BIND_HOST", DEFAULT_HOST),
        help="bind address for serve (default: AGORA_BIND_HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("PORT", str(DEFAULT_PORT)),
        type=_port,
        help="bind port for serve (default: PORT or 8000)",
    )
    parser.add_argument(
        "--workers",
        default=os.environ.get("AGORA_WORKERS", "1"),
        type=_workers,
        help=f"Uvicorn workers, 1-{MAX_WORKERS} (default: AGORA_WORKERS or 1)",
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.environ.get("AGORA_FORWARDED_ALLOW_IPS"),
        type=_forwarded_allow_ips,
        help=(
            "numeric proxy IP/CIDR allowlist (required for serve through "
            "AGORA_FORWARDED_ALLOW_IPS or this option)"
        ),
    )
    return parser


def _set_django_settings(service: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = SERVICES[service]["settings"]
    return environment


def _management_command(service: str, *arguments: str) -> list[str]:
    if service != "portal":
        raise SystemExit("prepare must run with the portal service mode")
    return [sys.executable, str(PROJECT_ROOT / "manage.py"), *arguments]


def _run_management(service: str, command: str) -> int:
    completed = subprocess.run(
        _management_command(service, command, "--noinput"),
        cwd=PROJECT_ROOT,
        env=_set_django_settings(service),
        check=False,
    )
    return completed.returncode


def _run_check(service: str) -> int:
    environment = _set_django_settings(service)
    arguments = ["check", "--fail-level", "ERROR"]
    if environment.get("AGORA_ENVIRONMENT") == "production":
        arguments = ["check", "--deploy", "--fail-level", "WARNING"]
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "manage.py"), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    return completed.returncode


def _run_prepare(service: str) -> int:
    if service != "portal":
        raise SystemExit("prepare must run with the portal service mode")
    check_return_code = _run_check(service)
    if check_return_code:
        return check_return_code
    for command in ("migrate", "collectstatic"):
        return_code = _run_management(service, command)
        if return_code:
            return return_code
    return 0


def _serve(service: str, host: str, port: int, workers: int, forwarded_allow_ips: str) -> None:
    os.environ["DJANGO_SETTINGS_MODULE"] = SERVICES[service]["settings"]
    os.environ["AGORA_FORWARDED_ALLOW_IPS"] = forwarded_allow_ips
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        SERVICES[service]["application"],
        "--host",
        host,
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--proxy-headers",
        "--forwarded-allow-ips",
        forwarded_allow_ips,
        "--no-server-header",
        "--timeout-graceful-shutdown",
        "30",
    ]
    if service == "content":
        # Render credentials are embedded in content request paths; never emit
        # those paths through Uvicorn's access logger.
        command.append("--no-access-log")
    else:
        command.append("--access-log")
    os.execv(sys.executable, command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.forwarded_allow_ips is None:
            parser.error("AGORA_FORWARDED_ALLOW_IPS is required for serve")
        check_return_code = _run_check(args.service)
        if check_return_code:
            return check_return_code
        _serve(args.service, args.host, args.port, args.workers, args.forwarded_allow_ips)
        return 0  # pragma: no cover - os.execv replaces this process
    return _run_prepare(args.service)


if __name__ == "__main__":
    raise SystemExit(main())
