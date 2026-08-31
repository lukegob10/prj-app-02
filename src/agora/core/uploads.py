"""Core-domain adapter for validated dashboard-package upload revisions."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from agora.core.models import Artifact, Revision
from agora.core.services import ArtifactPayload, create_complete_revision
from agora.core.storage import ArtifactStorage
from agora.uploads import (
    StagedUpload,
    UploadLimits,
    UploadPart,
    UploadRejected,
    prepare_upload,
    stage_upload,
)


def create_upload_revision(
    *,
    dashboard_id: UUID,
    created_by_id: UUID,
    parts: Iterable[UploadPart],
    storage: ArtifactStorage,
    limits: UploadLimits | None = None,
) -> Revision:
    """Validate, stage, and atomically hand one upload to the existing revision service."""

    staged = prepare_upload(parts, limits=limits)
    try:
        payloads = tuple(_payloads(staged))
        return create_complete_revision(
            dashboard_id=dashboard_id,
            created_by_id=created_by_id,
            payloads=payloads,
            storage=storage,
        )
    finally:
        staged.close()


def upload_revision(**kwargs: object) -> Revision:
    """Compatibility alias for the explicit revision-creation operation."""

    return create_upload_revision(**kwargs)  # type: ignore[arg-type]


def _payloads(staged: StagedUpload) -> Iterable[ArtifactPayload]:
    for file in staged.files:
        yield ArtifactPayload(
            kind=Artifact.Kind(file.kind.value),
            logical_name=file.logical_name.display,
            chunks=file.iter_chunks(),
            expected_size=file.byte_size,
            expected_sha256=file.sha256,
            media_type=file.media_type,
        )


__all__ = [
    "UploadLimits",
    "UploadPart",
    "UploadRejected",
    "create_upload_revision",
    "stage_upload",
    "upload_revision",
]
