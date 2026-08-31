"""Development-only browser refresh support for the trusted portal."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.http import require_GET

_WATCHED_DIRECTORIES = ("src", "scripts")
_IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})


def development_source_version(project_root: Path) -> str:
    """Return a stable metadata fingerprint for browser-visible development sources."""

    digest = sha256()
    for directory_name in _WATCHED_DIRECTORIES:
        root = project_root / directory_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if any(part in _IGNORED_DIRECTORY_NAMES for part in path.parts):
                continue
            if path.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            relative_path = path.relative_to(project_root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(f"\0{stat.st_mtime_ns}\0{stat.st_size}\0".encode())
    return digest.hexdigest()[:20]


@require_GET
def development_reload_version(request: HttpRequest) -> HttpResponse:
    """Expose the current source fingerprint only in the local development composition."""

    del request
    if not getattr(settings, "AGORA_DEVELOPMENT_LIVE_RELOAD", False):
        raise Http404
    response = HttpResponse(
        development_source_version(settings.BASE_DIR),
        content_type="text/plain; charset=utf-8",
    )
    response["Cache-Control"] = "no-store"
    return response
