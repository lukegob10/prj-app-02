"""Read-only artifact delivery for the isolated content entry point."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseNotAllowed,
    HttpResponseNotFound,
    StreamingHttpResponse,
)
from django.utils.cache import patch_vary_headers

from agora.persistence.models import Artifact
from agora.persistence.names import InvalidLogicalName, normalize_logical_name
from agora.persistence.storage import ArtifactStorageError, FilesystemArtifactStorage, StorageKey
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    resolve_render_authorization,
)

_STREAM_CHUNK_SIZE = 64 * 1024
_SUPPORTING_KINDS = (
    Artifact.Kind.CSV,
    Artifact.Kind.CSS,
    Artifact.Kind.IMAGE,
    Artifact.Kind.FONT,
)
_TEXT_MEDIA_TYPES = frozenset({"text/html", "text/csv", "text/css"})


def render_html(request: HttpRequest, audience: str, token: str) -> HttpResponseBase:
    """Deliver the exact authorized HTML artifact and nothing from portal state."""
    if request.method not in {"GET", "HEAD"}:
        return _render_failure(request, HttpResponseNotAllowed(["GET", "HEAD"]))
    try:
        target = resolve_render_authorization(token, audience=audience)
        artifact = target.revision.artifacts.get(kind=Artifact.Kind.HTML)
    except Artifact.DoesNotExist, Artifact.MultipleObjectsReturned, RenderAuthorizationDenied:
        return _render_failure(request, HttpResponseNotFound())
    return _artifact_response(request, artifact)


def render_csv(
    request: HttpRequest,
    audience: str,
    token: str,
    logical_name: str,
) -> HttpResponseBase:
    """Compatibility entry point for callers that explicitly request a CSV."""
    return _render_supporting_artifact(
        request,
        audience,
        token,
        logical_name,
        allowed_kinds=(Artifact.Kind.CSV,),
    )


def render_artifact(
    request: HttpRequest,
    audience: str,
    token: str,
    logical_name: str,
) -> HttpResponseBase:
    """Deliver one authorized same-revision supporting artifact by canonical metadata."""
    return _render_supporting_artifact(request, audience, token, logical_name)


def render_supporting_artifact(
    request: HttpRequest,
    audience: str,
    token: str,
    logical_name: str,
) -> HttpResponseBase:
    """Descriptive alias for the generic supporting-artifact delivery endpoint."""
    return render_artifact(request, audience, token, logical_name)


def _render_supporting_artifact(
    request: HttpRequest,
    audience: str,
    token: str,
    logical_name: str,
    *,
    allowed_kinds: tuple[Artifact.Kind, ...] = _SUPPORTING_KINDS,
) -> HttpResponseBase:
    """Resolve a same-revision supporting artifact, never by a filesystem path."""
    if request.method not in {"GET", "HEAD"}:
        return _render_failure(request, HttpResponseNotAllowed(["GET", "HEAD"]))
    try:
        normalized = normalize_logical_name(logical_name)
        target = resolve_render_authorization(token, audience=audience)
        artifact = target.revision.artifacts.get(
            kind__in=allowed_kinds,
            name_key=normalized.comparison_key,
        )
    except (
        Artifact.DoesNotExist,
        Artifact.MultipleObjectsReturned,
        InvalidLogicalName,
        RenderAuthorizationDenied,
    ):
        return _render_failure(request, HttpResponseNotFound())
    response = _artifact_response(request, artifact)
    if response.status_code < 400 and request.headers.get("Origin") == "null":
        response.headers["Access-Control-Allow-Origin"] = "null"
        patch_vary_headers(response, ("Origin",))
    return response


def _artifact_response(request: HttpRequest, artifact: Artifact) -> HttpResponseBase:
    storage = FilesystemArtifactStorage(Path(settings.AGORA_ARTIFACT_ROOT))
    try:
        key = StorageKey(artifact.storage_key)
        with storage.open(key):
            pass
    except ArtifactStorageError, FileNotFoundError:
        return _render_failure(request, HttpResponseNotFound())

    content_type = artifact.media_type
    if artifact.media_type in _TEXT_MEDIA_TYPES:
        content_type = f"{content_type}; charset=utf-8"
    if request.method == "HEAD":
        response: HttpResponseBase = HttpResponse(content_type=content_type)
    else:
        response = StreamingHttpResponse(
            _artifact_chunks(storage, key),
            content_type=content_type,
        )
    response.headers["Content-Length"] = str(artifact.byte_size)
    return response


def _artifact_chunks(storage: FilesystemArtifactStorage, key: StorageKey) -> Iterator[bytes]:
    with storage.open(key) as stream:
        while chunk := stream.read(_STREAM_CHUNK_SIZE):
            yield chunk


def _render_failure(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    """Keep short-lived bearer values out of Django's automatic 4xx request log."""
    request.path = "/render/<redacted>/"
    request.path_info = request.path
    return response


__all__ = [
    "render_artifact",
    "render_csv",
    "render_html",
    "render_supporting_artifact",
]
