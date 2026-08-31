"""Minimal unauthenticated liveness and readiness probes for both services."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from threading import Lock

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed

_PROBE_METHODS = ("GET", "HEAD")
_DATABASE_PROBE_LOCK = Lock()


def liveness(request: HttpRequest) -> HttpResponse:
    """Report process liveness without touching Oracle or private storage."""
    if request.method not in _PROBE_METHODS:
        return HttpResponseNotAllowed(_PROBE_METHODS)
    return _probe_response(healthy=True, ready_body="ok\n")


def portal_readiness(request: HttpRequest) -> HttpResponse:
    """Report whether the portal can reach metadata and read/write its artifact volume."""
    return _readiness(
        request,
        artifact_access=os.R_OK | os.W_OK | os.X_OK,
    )


def content_readiness(request: HttpRequest) -> HttpResponse:
    """Report whether content can reach metadata and read its artifact volume."""
    return _readiness(
        request,
        artifact_access=os.R_OK | os.X_OK,
    )


def _readiness(request: HttpRequest, *, artifact_access: int) -> HttpResponse:
    if request.method not in _PROBE_METHODS:
        return HttpResponseNotAllowed(_PROBE_METHODS)
    healthy = _database_is_usable() and _artifact_root_is_usable(artifact_access)
    return _probe_response(healthy=healthy, ready_body="ready\n")


def _probe_response(*, healthy: bool, ready_body: str) -> HttpResponse:
    """Return a stable response without exposing dependency errors or paths."""
    response = HttpResponse(
        ready_body if healthy else "not ready\n",
        status=200 if healthy else 503,
        content_type="text/plain; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _database_is_usable() -> bool:
    """Ping the configured database, converting all dependency failures to not-ready."""
    if not _DATABASE_PROBE_LOCK.acquire(blocking=False):
        return False
    try:
        connection.ensure_connection()
        return bool(connection.is_usable())
    except Exception:
        return False
    finally:
        _DATABASE_PROBE_LOCK.release()


def _artifact_root_is_usable(access: int) -> bool:
    """Check the configured private volume without creating files or reading artifact bytes."""
    try:
        root = Path(settings.AGORA_ARTIFACT_ROOT)
        entry = root.lstat()
        reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return (
            stat.S_ISDIR(entry.st_mode)
            and not (reparse_marker and getattr(entry, "st_file_attributes", 0) & reparse_marker)
            and os.access(root, access)
        )
    except OSError, TypeError, ValueError:
        return False


__all__ = ["content_readiness", "liveness", "portal_readiness"]
