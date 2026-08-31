"""Regression coverage for the persistent local reload host."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from django.test import Client, override_settings

from agora import development
from agora.portal.development import development_source_version


def test_windows_reload_replaces_only_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    reloader = object.__new__(development.DevelopmentReload)
    old_worker = Mock()
    new_worker = Mock()
    reloader.mtimes = {Path("changed.py"): 1.0}
    reloader.process = old_worker
    reloader.config = cast(Any, object())
    reloader.target = cast(Any, Mock())
    reloader.sockets = []

    get_subprocess = Mock(return_value=new_worker)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(development, "get_subprocess", get_subprocess)

    reloader.restart()

    assert reloader.mtimes == {}
    old_worker.terminate.assert_called_once_with()
    old_worker.join.assert_called_once_with()
    get_subprocess.assert_called_once_with(
        config=reloader.config,
        target=reloader.target,
        sockets=reloader.sockets,
    )
    new_worker.start.assert_called_once_with()


def test_development_server_keeps_the_bound_socket_in_its_reloader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = Mock()
    socket = Mock()
    config.bind_socket.return_value = socket
    server = Mock()
    reloader = Mock()
    config_factory = Mock(return_value=config)
    server_factory = Mock(return_value=server)
    reloader_factory = Mock(return_value=reloader)
    bind_socket = Mock(return_value=socket)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(development, "Config", config_factory)
    monkeypatch.setattr(development, "Server", server_factory)
    monkeypatch.setattr(development, "DevelopmentReload", reloader_factory)
    monkeypatch.setattr(development, "_bind_development_socket", bind_socket)

    app_dir = tmp_path / "scripts"
    source_dir = tmp_path / "src"
    certificate = tmp_path / "localhost.pem"
    private_key = tmp_path / "localhost.key"
    development.run_development_server(
        "run_https:application",
        app_dir=app_dir,
        host="127.0.0.1",
        port=8443,
        reload_dirs=[source_dir, app_dir],
        ssl_certfile=certificate,
        ssl_keyfile=private_key,
        access_log=False,
    )

    assert sys.path[0] == str(app_dir)
    config_factory.assert_called_once_with(
        "run_https:application",
        host="127.0.0.1",
        port=8443,
        reload=True,
        reload_dirs=[str(source_dir), str(app_dir)],
        ssl_certfile=str(certificate),
        ssl_keyfile=str(private_key),
        access_log=False,
    )
    server_factory.assert_called_once_with(config=config)
    bind_socket.assert_called_once_with(config)
    reloader_factory.assert_called_once_with(config, target=server.run, sockets=[socket])
    reloader.run.assert_called_once_with()


def test_windows_development_socket_uses_exclusive_address_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Mock(host="127.0.0.1", port=8443)
    bound_socket = Mock()
    socket_factory = Mock(return_value=bound_socket)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(development.socket, "socket", socket_factory)

    assert development._bind_development_socket(config) is bound_socket

    socket_factory.assert_called_once_with(
        family=development.socket.AF_INET,
        type=development.socket.SOCK_STREAM,
    )
    bound_socket.setsockopt.assert_called_once_with(
        development.socket.SOL_SOCKET,
        development.socket.SO_EXCLUSIVEADDRUSE,
        1,
    )
    bound_socket.bind.assert_called_once_with(("127.0.0.1", 8443))
    bound_socket.set_inheritable.assert_called_once_with(True)


def test_development_source_version_tracks_sources_and_ignores_bytecode(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "agora"
    source_root.mkdir(parents=True)
    source = source_root / "template.html"
    source.write_text("first", encoding="utf-8")
    initial = development_source_version(tmp_path)

    source.write_text("second version", encoding="utf-8")
    changed = development_source_version(tmp_path)

    bytecode_root = source_root / "__pycache__"
    bytecode_root.mkdir()
    (bytecode_root / "ignored.pyc").write_bytes(b"runtime noise")

    assert changed != initial
    assert development_source_version(tmp_path) == changed


@override_settings(AGORA_DEVELOPMENT_LIVE_RELOAD=True)
def test_development_reload_endpoint_returns_the_current_version(client: Client) -> None:
    response = client.get("/__dev__/reload/", HTTP_HOST="portal.agora.test")

    assert response.status_code == 200
    assert len(response.content.decode()) == 20
    assert response["Cache-Control"] == "no-store"
    assert "script-src 'self'" in response["Content-Security-Policy"]


@override_settings(AGORA_DEVELOPMENT_LIVE_RELOAD=False)
def test_development_reload_endpoint_is_absent_when_disabled(client: Client) -> None:
    assert client.get("/__dev__/reload/", HTTP_HOST="portal.agora.test").status_code == 404
