"""Uvicorn development runner with reload isolation for Windows process trees."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

from uvicorn import Config, Server
from uvicorn._subprocess import get_subprocess
from uvicorn.supervisors.statreload import StatReload


class DevelopmentReload(StatReload):
    """Reload one worker without broadcasting Ctrl+C to sibling services on Windows."""

    def restart(self) -> None:
        if sys.platform != "win32":
            super().restart()
            return

        self.mtimes.clear()
        self.process.terminate()
        self.process.join()
        self.process = get_subprocess(
            config=self.config,
            target=self.target,
            sockets=self.sockets,
        )
        self.process.start()


def _bind_development_socket(config: Config) -> socket.socket:
    """Bind exclusively on Windows so stale workers cannot share a local service port."""

    if sys.platform != "win32":
        return config.bind_socket()

    host = config.host or "127.0.0.1"
    port = config.port or 0
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bound_socket = socket.socket(family=family, type=socket.SOCK_STREAM)
    try:
        bound_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        bound_socket.bind((host, port))
        bound_socket.set_inheritable(True)
    except OSError:
        bound_socket.close()
        raise
    return bound_socket


def run_development_server(
    app: str,
    *,
    app_dir: Path,
    host: str,
    port: int,
    reload_dirs: list[Path],
    ssl_certfile: Path,
    ssl_keyfile: Path,
    access_log: bool = True,
) -> None:
    """Run one reloadable HTTPS service while its bound socket remains stable."""

    app_directory = str(app_dir)
    if app_directory not in sys.path:
        sys.path.insert(0, app_directory)

    config = Config(
        app,
        host=host,
        port=port,
        reload=True,
        reload_dirs=[str(path) for path in reload_dirs],
        ssl_certfile=str(ssl_certfile),
        ssl_keyfile=str(ssl_keyfile),
        access_log=access_log,
    )
    server = Server(config=config)
    bound_socket = _bind_development_socket(config)
    DevelopmentReload(config, target=server.run, sockets=[bound_socket]).run()
