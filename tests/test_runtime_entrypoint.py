from __future__ import annotations

import argparse
import os
import subprocess
from types import SimpleNamespace
from typing import cast

import pytest

import deploy.entrypoint as entrypoint


def test_service_defaults_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGORA_BIND_HOST", raising=False)
    monkeypatch.delenv("AGORA_WORKERS", raising=False)
    monkeypatch.delenv("AGORA_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    portal = entrypoint._parser().parse_args(["portal"])
    content = entrypoint._parser().parse_args(["content"])

    assert portal.command == "serve"
    assert content.command == "serve"
    assert portal.workers == content.workers == 1
    assert portal.forwarded_allow_ips is None
    assert content.forwarded_allow_ips is None


def test_runtime_environment_controls_bind_and_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGORA_BIND_HOST", "127.0.0.2")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("AGORA_WORKERS", "3")
    monkeypatch.setenv("AGORA_FORWARDED_ALLOW_IPS", "10.1.2.3,10.20.1.4/16")

    args = entrypoint._parser().parse_args(["portal", "serve"])

    assert args.host == "127.0.0.2"
    assert args.port == 8080
    assert args.workers == 3
    assert args.forwarded_allow_ips == "10.1.2.3,10.20.0.0/16"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "*",
        "0.0.0.0/0",
        "::/0",
        "proxy.internal",
        "127.0.0.1,*",
        "127.0.0.1, ",
    ],
)
def test_forwarded_proxy_allowlist_rejects_implicit_trust(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        entrypoint._forwarded_allow_ips(value)


@pytest.mark.parametrize("value", ["0", "33", "many"])
def test_worker_count_is_bounded(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        entrypoint._workers(value)


def test_prepare_runs_migration_before_static_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        environment = cast(dict[str, str], kwargs["env"])
        calls.append((command[2:], environment["DJANGO_SETTINGS_MODULE"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("AGORA_ENVIRONMENT", "production")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert entrypoint._run_prepare("portal") == 0
    assert calls == [
        (["check", "--deploy", "--fail-level", "WARNING"], "agora.settings.portal"),
        (["migrate", "--noinput"], "agora.settings.portal"),
        (["collectstatic", "--noinput"], "agora.settings.portal"),
    ]


def test_serve_requires_explicit_proxy_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGORA_FORWARDED_ALLOW_IPS", raising=False)

    with pytest.raises(SystemExit):
        entrypoint.main(["portal", "serve"])


def test_serve_stops_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGORA_FORWARDED_ALLOW_IPS", "10.0.0.1")
    monkeypatch.setattr(entrypoint, "_run_check", lambda service: 3)
    monkeypatch.setattr(
        entrypoint,
        "_serve",
        lambda *args: pytest.fail("serve must not start after a failed preflight"),
    )

    assert entrypoint.main(["portal", "serve"]) == 3


def test_service_mode_overrides_external_settings_and_redacts_content_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, list[str]]] = []

    def fake_exec(executable: str, command: list[str]) -> None:
        del executable
        captured.append((os.environ["DJANGO_SETTINGS_MODULE"], command))

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "wrong.settings")
    monkeypatch.setattr(os, "execv", fake_exec)

    entrypoint._serve("content", "0.0.0.0", 8001, 1, "10.0.0.0/8")
    entrypoint._serve("portal", "0.0.0.0", 8000, 2, "127.0.0.1")

    assert captured[0][0] == "agora.settings.content"
    assert "--no-access-log" in captured[0][1]
    assert "--forwarded-allow-ips" in captured[0][1]
    assert "--no-server-header" in captured[0][1]
    assert captured[1][0] == "agora.settings.portal"
    assert "--access-log" in captured[1][1]
    assert "--workers" in captured[1][1]
