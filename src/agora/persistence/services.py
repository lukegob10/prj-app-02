"""Narrow transactional services for complete revisions and cleanup ownership."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from agora.persistence.models import (
    Artifact,
    AuditEvent,
    Dashboard,
    Revision,
    StorageReservation,
    User,
)
from agora.persistence.names import LogicalName, normalize_logical_name
from agora.persistence.storage import (
    ArtifactStorage,
    ArtifactStorageError,
    StorageCollision,
    StorageKey,
    StorageWriteCommitted,
    StoredArtifact,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_RESERVATION_LIFETIME: Final = timedelta(hours=24)
_KEY_ATTEMPTS: Final = 8


class RevisionCreationError(RuntimeError):
    """Raised for a rejected complete-revision operation."""


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """A one-shot verified byte stream with logical, never physical, naming."""

    kind: Artifact.Kind
    logical_name: str
    chunks: Iterable[bytes]
    expected_size: int
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class CleanupResult:
    removed: int
    retained: int


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    reservation_id: uuid.UUID
    name: LogicalName
    payload: ArtifactPayload
    receipt: StoredArtifact


@dataclass(slots=True)
class _CleanupCandidate:
    reservation_id: uuid.UUID
    key: StorageKey
    owns_bytes: bool = False


def create_complete_revision(
    *,
    dashboard_id: uuid.UUID,
    created_by_id: uuid.UUID,
    payloads: Sequence[ArtifactPayload],
    storage: ArtifactStorage,
) -> Revision:
    """Durably store bytes, then atomically expose one complete immutable Revision."""
    if connection.in_atomic_block:
        raise RevisionCreationError("revision creation must own its database commit boundary")
    normalized = _validate_payload_set(payloads)
    if not Dashboard.objects.filter(
        id=dashboard_id,
        owner_id=created_by_id,
        owner__is_active=True,
        state__in=_revision_accepting_states(),
    ).exists():
        raise RevisionCreationError(
            "revision creator must own an active revision-accepting dashboard"
        )
    reservations: list[_CleanupCandidate] = []
    prepared: list[_PreparedArtifact] = []
    try:
        for payload, name in zip(payloads, normalized, strict=True):
            reservation = _reserve_storage_key(storage)
            key = StorageKey(reservation.storage_key)
            cleanup = _CleanupCandidate(reservation.id, key)
            reservations.append(cleanup)
            try:
                receipt = storage.write(
                    key,
                    payload.chunks,
                    expected_size=payload.expected_size,
                    expected_sha256=payload.expected_sha256,
                )
            except StorageCollision:
                try:
                    _mark_reservation_collision(reservation.id)
                except BaseException:
                    pass
                raise
            except StorageWriteCommitted as error:
                cleanup.owns_bytes = True
                try:
                    _mark_reservation_verified(reservation.id, error.receipt)
                except BaseException:
                    pass
                raise
            cleanup.owns_bytes = True
            _mark_reservation_verified(reservation.id, receipt)
            prepared.append(
                _PreparedArtifact(
                    reservation_id=reservation.id,
                    name=name,
                    payload=payload,
                    receipt=receipt,
                )
            )

        revision = _commit_revision_metadata(
            dashboard_id=dashboard_id,
            created_by_id=created_by_id,
            prepared=prepared,
        )
    except BaseException:
        _cleanup_unowned(storage, reservations)
        raise
    return revision


def cleanup_expired_reservations(
    storage: ArtifactStorage, *, now: datetime | None = None, limit: int = 100
) -> CleanupResult:
    """Idempotently remove bounded expired writes that no Artifact owns."""
    if connection.in_atomic_block:
        raise ArtifactStorageError("reservation cleanup must own its database commit boundary")
    if limit < 1:
        raise ValueError("cleanup limit must be positive")
    cutoff = now or timezone.now()
    reservation_ids = list(
        StorageReservation.objects.filter(expires_at__lte=cutoff)
        .order_by("expires_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    removed = 0
    retained = 0
    for reservation_id in reservation_ids:
        with transaction.atomic(durable=True):
            reservation = (
                StorageReservation.objects.select_for_update()
                .filter(id=reservation_id, expires_at__lte=cutoff)
                .first()
            )
            if reservation is None:
                continue
            if Artifact.objects.filter(storage_key=reservation.storage_key).exists():
                reservation.delete()
                removed += 1
                continue
            if reservation.storage_state == StorageReservation.StorageState.COLLISION:
                reservation.delete()
                removed += 1
                continue
            if reservation.storage_state == StorageReservation.StorageState.RESERVED:
                reservation.cleanup_required = True
                reservation.save(update_fields=("cleanup_required",))
                retained += 1
                continue
            try:
                storage.delete(StorageKey(reservation.storage_key))
            except ArtifactStorageError:
                reservation.cleanup_required = True
                reservation.save(update_fields=("cleanup_required",))
                retained += 1
            else:
                reservation.delete()
                removed += 1
    return CleanupResult(removed=removed, retained=retained)


def _validate_payload_set(payloads: Sequence[ArtifactPayload]) -> list[LogicalName]:
    if sum(payload.kind == Artifact.Kind.HTML for payload in payloads) != 1:
        raise RevisionCreationError("a complete revision requires exactly one HTML artifact")
    names: list[LogicalName] = []
    seen: set[str] = set()
    for payload in payloads:
        if payload.kind not in Artifact.Kind.values:
            raise RevisionCreationError("artifact kind is not supported")
        if payload.expected_size < 0:
            raise RevisionCreationError("artifact size must be nonnegative")
        if _DIGEST_RE.fullmatch(payload.expected_sha256) is None:
            raise RevisionCreationError("artifact SHA-256 must be lowercase hexadecimal")
        name = normalize_logical_name(payload.logical_name)
        if name.comparison_key in seen:
            raise RevisionCreationError("artifact logical names must be unique after normalization")
        seen.add(name.comparison_key)
        names.append(name)
    return names


def _reserve_storage_key(storage: ArtifactStorage) -> StorageReservation:
    for _ in range(_KEY_ATTEMPTS):
        key = storage.generate_key()
        if Artifact.objects.filter(storage_key=key.value).exists():
            continue
        try:
            with transaction.atomic(durable=True):
                return StorageReservation.objects.create(
                    storage_key=key.value,
                    expires_at=timezone.now() + _RESERVATION_LIFETIME,
                )
        except IntegrityError:
            continue
    raise RevisionCreationError("could not allocate a unique artifact storage key")


def _mark_reservation_verified(reservation_id: uuid.UUID, receipt: StoredArtifact) -> None:
    with transaction.atomic(durable=True):
        reservation = StorageReservation.objects.select_for_update().get(id=reservation_id)
        if reservation.storage_key != receipt.key.value:
            raise RevisionCreationError("storage receipt did not match its reservation")
        reservation.verified_size = receipt.byte_size
        reservation.verified_sha256 = receipt.sha256
        reservation.storage_state = StorageReservation.StorageState.OWNED
        reservation.cleanup_required = False
        reservation.save(
            update_fields=(
                "verified_size",
                "verified_sha256",
                "storage_state",
                "cleanup_required",
            )
        )


def _mark_reservation_collision(reservation_id: uuid.UUID) -> None:
    with transaction.atomic(durable=True):
        reservation = StorageReservation.objects.select_for_update().get(id=reservation_id)
        if reservation.storage_state == StorageReservation.StorageState.COLLISION:
            return
        if reservation.storage_state != StorageReservation.StorageState.RESERVED:
            raise RevisionCreationError("verified storage reservation cannot become a collision")
        reservation.storage_state = StorageReservation.StorageState.COLLISION
        reservation.save(update_fields=("storage_state",))


def _commit_revision_metadata(
    *,
    dashboard_id: uuid.UUID,
    created_by_id: uuid.UUID,
    prepared: Sequence[_PreparedArtifact],
) -> Revision:
    with transaction.atomic(durable=True):
        creator = (
            User.objects.select_for_update()
            .filter(id=created_by_id)
            .only("id", "is_active")
            .first()
        )
        if creator is None or not creator.is_active:
            raise RevisionCreationError("revision creator must be active")
        dashboard = Dashboard.objects.select_for_update().filter(id=dashboard_id).first()
        if dashboard is None or dashboard.owner_id != created_by_id:
            raise RevisionCreationError("revision creator must own the dashboard")
        if dashboard.state not in _revision_accepting_states():
            raise RevisionCreationError("dashboard state does not accept revisions")

        reservation_ids = [item.reservation_id for item in prepared]
        reservations = {
            reservation.id: reservation
            for reservation in StorageReservation.objects.select_for_update().filter(
                id__in=reservation_ids
            )
        }
        if len(reservations) != len(prepared):
            raise RevisionCreationError("artifact storage reservation is missing")
        for item in prepared:
            reservation = reservations[item.reservation_id]
            if (
                reservation.storage_key != item.receipt.key.value
                or reservation.verified_size != item.receipt.byte_size
                or reservation.verified_sha256 != item.receipt.sha256
                or reservation.cleanup_required
            ):
                raise RevisionCreationError("artifact storage reservation is not verified")
            if Artifact.objects.filter(storage_key=item.receipt.key.value).exists():
                raise RevisionCreationError("artifact storage key is already owned")

        next_number = 1
        if dashboard.latest_revision is not None:
            next_number = dashboard.latest_revision.number + 1
        revision = Revision(
            dashboard=dashboard,
            number=next_number,
            created_by_id=created_by_id,
        )
        revision.save(force_insert=True)
        for item in prepared:
            Artifact(
                revision=revision,
                kind=item.payload.kind,
                logical_name=item.name.display,
                name_key=item.name.comparison_key,
                storage_key=item.receipt.key.value,
                byte_size=item.receipt.byte_size,
                media_type=("text/html" if item.payload.kind == Artifact.Kind.HTML else "text/csv"),
                sha256=item.receipt.sha256,
            ).save(force_insert=True)

        locked = Revision.objects.filter(id=revision.id, artifacts_locked=False).update(
            artifacts_locked=True
        )
        if locked != 1:
            raise RevisionCreationError("revision artifacts could not be finalized")
        revision.artifacts_locked = True

        dashboard.latest_revision = revision
        dashboard.save(update_fields=("latest_revision", "updated_at"))
        AuditEvent(
            event_type="revision.created",
            actor_id=created_by_id,
            dashboard=dashboard,
            revision=revision,
            metadata={"artifact_count": len(prepared), "revision_number": next_number},
        ).save(force_insert=True)
        StorageReservation.objects.filter(id__in=reservation_ids).delete()
        return revision


def _cleanup_unowned(storage: ArtifactStorage, reservations: Sequence[_CleanupCandidate]) -> None:
    for candidate in reservations:
        try:
            with transaction.atomic(durable=True):
                reservation = (
                    StorageReservation.objects.select_for_update()
                    .filter(id=candidate.reservation_id)
                    .first()
                )
                if reservation is None:
                    continue
                if reservation.storage_key != candidate.key.value:
                    reservation.cleanup_required = True
                    reservation.save(update_fields=("cleanup_required",))
                    continue
                if Artifact.objects.filter(storage_key=candidate.key.value).exists():
                    reservation.delete()
                    continue
                if reservation.storage_state == StorageReservation.StorageState.COLLISION:
                    reservation.delete()
                    continue
                if not candidate.owns_bytes:
                    reservation.delete()
                    continue
                try:
                    storage.delete(candidate.key)
                except ArtifactStorageError:
                    reservation.cleanup_required = True
                    reservation.save(update_fields=("cleanup_required",))
                else:
                    reservation.delete()
        except BaseException:
            continue


def _revision_accepting_states() -> tuple[str, str, str]:
    return (
        Dashboard.State.DRAFT,
        Dashboard.State.PUBLISHED,
        Dashboard.State.UNPUBLISHED,
    )
