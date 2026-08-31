from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

import agora.core.services as core_services
from agora.core.models import (
    Artifact,
    AuditEvent,
    Dashboard,
    ImmutableRecordError,
    Revision,
    StorageReservation,
    User,
    ViewerGrant,
)
from agora.core.names import normalize_logical_name
from agora.core.services import (
    ArtifactPayload,
    RevisionCreationError,
    cleanup_expired_reservations,
    create_complete_revision,
)
from agora.core.storage import (
    ArtifactStorageError,
    FilesystemArtifactStorage,
    StorageCollision,
    StorageKey,
    StoredArtifact,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner() -> User:
    return User.objects.create_user(" owner.1 ")


@pytest.fixture
def viewer() -> User:
    return User.objects.create_user("viewer.1")


@pytest.fixture
def dashboard(owner: User) -> Dashboard:
    return Dashboard.objects.create(owner=owner, name="Quarterly risk")


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemArtifactStorage:
    return FilesystemArtifactStorage(tmp_path / "private-artifacts")


def payload(kind: Artifact.Kind, name: str, content: bytes) -> ArtifactPayload:
    midpoint = len(content) // 2
    return ArtifactPayload(
        kind=kind,
        logical_name=name,
        chunks=(content[:midpoint], content[midpoint:]),
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )


def create_revision(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> Revision:
    return create_complete_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        payloads=(
            payload(Artifact.Kind.HTML, "dashboard.html", b"<html>safe bytes</html>"),
            payload(Artifact.Kind.CSV, "positions.csv", b"desk,value\nA,1\n"),
        ),
        storage=storage,
    )


def test_user_manager_canonicalizes_and_database_rejects_duplicates(owner: User) -> None:
    assert owner.soeid == "OWNER.1"
    assert str(owner) == "OWNER.1"
    assert not owner.has_usable_password()

    with pytest.raises(ValueError, match="AG-003"):
        User.objects.create_user("NEW.USER", password="not-provisioned-here")
    with pytest.raises(ValueError, match="AG-003"):
        User.objects.create_superuser("ADMIN")
    with pytest.raises(ValidationError, match="canonical"):
        User(soeid="lowercase").full_clean()
    with pytest.raises(ValidationError):
        User.objects.create_user(" owner.1 ")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.bulk_create([User(soeid="OWNER.1")])
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.bulk_create([User(soeid="lowercase")])


def test_user_identity_and_hard_deletion_are_guarded(owner: User) -> None:
    owner.soeid = "CHANGED"
    with pytest.raises(ImmutableRecordError):
        owner.save()
    owner.refresh_from_db()
    with pytest.raises(ImmutableRecordError):
        owner.delete()

    unused = User.objects.create_user("UNUSED")
    with pytest.raises(DatabaseError), transaction.atomic():
        User.objects.filter(id=unused.id).delete()


def test_dashboard_ownership_and_publication_state_are_constrained(
    dashboard: Dashboard, viewer: User
) -> None:
    assert str(dashboard) == "Quarterly risk"
    with pytest.raises(ImmutableRecordError, match="soft-deleted"):
        dashboard.delete()
    dashboard.owner = viewer
    with pytest.raises(ImmutableRecordError):
        dashboard.save()
    dashboard.refresh_from_db()

    with pytest.raises(DatabaseError), transaction.atomic():
        Dashboard.objects.filter(id=dashboard.id).update(owner=viewer)
    with pytest.raises(IntegrityError), transaction.atomic():
        Dashboard.objects.filter(id=dashboard.id).update(state=Dashboard.State.PUBLISHED)


def test_dashboard_lifecycle_history_restore_and_terminal_state_are_enforced(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        Dashboard.objects.bulk_create(
            [Dashboard(owner=owner, name="Invalid origin", state=Dashboard.State.ARCHIVED)]
        )

    dashboard.state = Dashboard.State.ARCHIVED
    dashboard.save()
    dashboard.state = Dashboard.State.UNPUBLISHED
    with pytest.raises(ValidationError, match="transition is not allowed"):
        dashboard.save()
    dashboard.refresh_from_db()
    dashboard.state = Dashboard.State.DRAFT
    dashboard.save()

    revision = create_revision(dashboard, owner, storage)
    dashboard.refresh_from_db()
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.published_revision = revision
    dashboard.first_published_at = timezone.now()
    dashboard.save()
    first_published_at = dashboard.first_published_at

    dashboard.state = Dashboard.State.ARCHIVED
    dashboard.published_revision = None
    dashboard.save()
    dashboard.state = Dashboard.State.DRAFT
    with pytest.raises(ValidationError, match="transition is not allowed"):
        dashboard.save()
    dashboard.refresh_from_db()
    dashboard.state = Dashboard.State.UNPUBLISHED
    dashboard.save()
    assert dashboard.first_published_at == first_published_at

    dashboard.state = Dashboard.State.DELETED
    dashboard.save()
    dashboard.name = "Changed tombstone"
    with pytest.raises(ImmutableRecordError, match="terminal"):
        dashboard.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        Dashboard.objects.filter(id=dashboard.id).update(name="Raw tombstone change")


def test_complete_revision_commits_metadata_bytes_latest_pointer_and_audit(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    revision = create_revision(dashboard, owner, storage)

    dashboard.refresh_from_db()
    revision.refresh_from_db()
    artifacts = list(revision.artifacts.order_by("kind", "logical_name"))
    assert revision.number == 1
    assert revision.artifacts_locked is True
    assert dashboard.latest_revision_id == revision.id
    assert dashboard.published_revision_id is None
    assert [(item.kind, item.logical_name) for item in artifacts] == [
        (Artifact.Kind.CSV, "positions.csv"),
        (Artifact.Kind.HTML, "dashboard.html"),
    ]
    assert all(item.storage_key != item.logical_name for item in artifacts)
    assert StorageReservation.objects.count() == 0
    event = AuditEvent.objects.get()
    assert event.event_type == "revision.created"
    assert event.actor_id == owner.id
    assert event.metadata == {"artifact_count": 2, "revision_number": 1}
    for item in artifacts:
        with storage.open(StorageKey(item.storage_key)) as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == item.sha256
    assert not hasattr(storage, "url")
    assert not hasattr(storage, "path")


def test_later_revision_does_not_move_publication(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    first = create_revision(dashboard, owner, storage)
    dashboard.refresh_from_db()
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.published_revision = first
    dashboard.first_published_at = timezone.now()
    dashboard.save()

    second = create_complete_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        payloads=(payload(Artifact.Kind.HTML, "new.html", b"<html>new</html>"),),
        storage=storage,
    )

    dashboard.refresh_from_db()
    assert second.number == 2
    assert dashboard.latest_revision_id == second.id
    assert dashboard.published_revision_id == first.id
    assert dashboard.state == Dashboard.State.PUBLISHED


def test_complete_revision_rejects_wrong_owner_without_leaking_files(
    dashboard: Dashboard, viewer: User, storage: FilesystemArtifactStorage
) -> None:
    with pytest.raises(RevisionCreationError, match="must own"):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=viewer.id,
            payloads=(payload(Artifact.Kind.HTML, "dashboard.html", b"<html></html>"),),
            storage=storage,
        )

    assert Revision.objects.count() == 0
    assert Artifact.objects.count() == 0
    assert StorageReservation.objects.count() == 0
    assert list(storage._root.rglob("*.*")) == []


def test_complete_revision_rejects_disabled_owner_before_storage(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    owner.is_active = False
    owner.save(update_fields=("is_active", "updated_at"))

    with pytest.raises(RevisionCreationError, match="active"):
        create_revision(dashboard, owner, storage)
    with pytest.raises(DatabaseError), transaction.atomic():
        Revision.objects.bulk_create([Revision(dashboard=dashboard, number=1, created_by=owner)])

    assert StorageReservation.objects.count() == 0
    assert list(storage._root.rglob("*.*")) == []


@pytest.mark.parametrize("state", [Dashboard.State.ARCHIVED, Dashboard.State.DELETED])
def test_complete_revision_rejects_terminal_or_read_only_dashboard_state(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    state: Dashboard.State,
) -> None:
    dashboard.state = state
    dashboard.save()

    with pytest.raises(RevisionCreationError, match="revision-accepting"):
        create_revision(dashboard, owner, storage)
    with pytest.raises(DatabaseError), transaction.atomic():
        Revision.objects.bulk_create([Revision(dashboard=dashboard, number=1, created_by=owner)])

    assert StorageReservation.objects.count() == 0


def test_dashboard_state_is_rechecked_after_streaming_begins(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    content = b"<html>streamed</html>"

    def chunks() -> Iterator[bytes]:
        yield content[:6]
        Dashboard.objects.filter(id=dashboard.id).update(state=Dashboard.State.ARCHIVED)
        yield content[6:]

    streamed = ArtifactPayload(
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        chunks=chunks(),
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(RevisionCreationError, match="does not accept revisions"):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=(streamed,),
            storage=storage,
        )

    assert Revision.objects.count() == 0
    assert Artifact.objects.count() == 0
    assert StorageReservation.objects.count() == 0
    assert list(storage._root.rglob("*.*")) == []


@pytest.mark.parametrize("html_count", [0, 2])
def test_payload_set_requires_exactly_one_html_before_reserving_storage(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    html_count: int,
) -> None:
    payloads = tuple(
        payload(Artifact.Kind.HTML, f"dashboard-{number}.html", b"<html></html>")
        for number in range(html_count)
    )

    with pytest.raises(RevisionCreationError, match="exactly one HTML"):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=payloads,
            storage=storage,
        )

    assert StorageReservation.objects.count() == 0


@pytest.mark.parametrize(
    ("first", "second"),
    [("café.csv", "café.csv"), ("Report.csv", "report.CSV"), ("\uff2b.csv", "k.csv")],
)
def test_unicode_name_collisions_fail_before_writing(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    first: str,
    second: str,
) -> None:
    with pytest.raises(RevisionCreationError, match="unique after normalization"):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=(
                payload(Artifact.Kind.HTML, "dashboard.html", b"<html></html>"),
                payload(Artifact.Kind.CSV, first, b"a\n1\n"),
                payload(Artifact.Kind.CSV, second, b"a\n2\n"),
            ),
            storage=storage,
        )

    assert StorageReservation.objects.count() == 0


@pytest.mark.parametrize(
    "logical_name", ["cafe\u0301.csv", "Straße.csv", "\u212a.csv", "\uff2b.csv"]
)
def test_unicode_canonical_names_persist_through_oracle(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    logical_name: str,
) -> None:
    revision = create_complete_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        payloads=(
            payload(Artifact.Kind.HTML, "dashboard.html", b"<html></html>"),
            payload(Artifact.Kind.CSV, logical_name, b"a\n1\n"),
        ),
        storage=storage,
    )

    artifact = revision.artifacts.get(kind=Artifact.Kind.CSV)
    expected = normalize_logical_name(logical_name)
    assert artifact.logical_name == expected.display
    assert artifact.name_key == expected.comparison_key


def test_stream_failure_removes_reservation_and_partial_bytes(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
) -> None:
    content = b"<html>partial"

    def broken_stream() -> Iterator[bytes]:
        yield content
        raise RuntimeError("simulated disconnect")

    failing = ArtifactPayload(
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        chunks=broken_stream(),
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(RuntimeError, match="disconnect"):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=(failing,),
            storage=storage,
        )

    assert StorageReservation.objects.count() == 0
    assert Revision.objects.count() == 0
    assert list(storage._root.rglob("*.pending")) == []


def test_metadata_failure_compensates_every_completed_file(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_commit(**kwargs: object) -> Revision:
        raise IntegrityError("simulated metadata failure")

    monkeypatch.setattr(core_services, "_commit_revision_metadata", fail_commit)
    with pytest.raises(IntegrityError, match="metadata failure"):
        create_revision(dashboard, owner, storage)

    assert StorageReservation.objects.count() == 0
    assert Revision.objects.count() == 0
    assert list(storage._root.rglob("*.pending")) == []
    assert [path for path in storage._root.rglob("*") if path.is_file()] == []


def test_revision_and_cleanup_services_require_an_outermost_commit_boundary(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    with transaction.atomic():
        with pytest.raises(RevisionCreationError, match="commit boundary"):
            create_revision(dashboard, owner, storage)
        with pytest.raises(ArtifactStorageError, match="commit boundary"):
            cleanup_expired_reservations(storage)

    with pytest.raises(ValueError, match="positive"):
        cleanup_expired_reservations(storage, limit=0)


def test_payload_metadata_is_rejected_before_storage(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    html = payload(Artifact.Kind.HTML, "dashboard.html", b"<html></html>")
    invalid_kind = ArtifactPayload(
        kind=cast(Artifact.Kind, "pdf"),
        logical_name="document.pdf",
        chunks=(b"bytes",),
        expected_size=5,
        expected_sha256=hashlib.sha256(b"bytes").hexdigest(),
    )
    negative_size = ArtifactPayload(
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        chunks=(),
        expected_size=-1,
        expected_sha256=hashlib.sha256(b"").hexdigest(),
    )
    invalid_digest = ArtifactPayload(
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        chunks=(),
        expected_size=0,
        expected_sha256="A" * 64,
    )

    for candidate, message in (
        ((html, invalid_kind), "kind"),
        ((negative_size,), "size"),
        ((invalid_digest,), "SHA-256"),
    ):
        with pytest.raises(RevisionCreationError, match=message):
            create_complete_revision(
                dashboard_id=dashboard.id,
                created_by_id=owner.id,
                payloads=candidate,
                storage=storage,
            )
    assert StorageReservation.objects.count() == 0


class FixedKeyStorage(FilesystemArtifactStorage):
    def __init__(self, root: Path, key: StorageKey) -> None:
        super().__init__(root)
        self.key = key

    def generate_key(self) -> StorageKey:
        return self.key


def test_storage_reservation_retries_both_metadata_collision_types(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    tmp_path: Path,
) -> None:
    revision = create_revision(dashboard, owner, storage)
    owned_key = StorageKey(revision.artifacts.get(kind=Artifact.Kind.HTML).storage_key)
    with pytest.raises(RevisionCreationError, match="unique artifact storage key"):
        core_services._reserve_storage_key(FixedKeyStorage(tmp_path / "owned-key", owned_key))

    reserved_key = StorageKey.generate()
    StorageReservation.objects.create(
        storage_key=reserved_key.value,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    with pytest.raises(RevisionCreationError, match="unique artifact storage key"):
        core_services._reserve_storage_key(FixedKeyStorage(tmp_path / "reserved-key", reserved_key))


def test_service_collision_preserves_bytes_owned_by_another_attempt(
    dashboard: Dashboard, owner: User, tmp_path: Path
) -> None:
    key = StorageKey("v1/dd/dd/" + "d" * 64)
    storage = FixedKeyStorage(tmp_path / "private", key)
    storage.write(key, (b"pre-existing bytes",))

    with pytest.raises(StorageCollision):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=(payload(Artifact.Kind.HTML, "dashboard.html", b"new bytes"),),
            storage=storage,
        )

    with storage.open(key) as stream:
        assert stream.read() == b"pre-existing bytes"
    assert StorageReservation.objects.count() == 0
    assert Revision.objects.count() == 0


def test_collision_bytes_survive_failed_compensation_and_expiry(
    dashboard: Dashboard,
    owner: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = StorageKey("v1/ee/ee/" + "e" * 64)
    storage = FixedKeyStorage(tmp_path / "private", key)
    storage.write(key, (b"pre-existing bytes",))

    def fail_collision_witness(reservation_id: uuid.UUID) -> None:
        del reservation_id
        raise DatabaseError("simulated witness database failure")

    def lose_immediate_compensation(storage_arg: object, reservations: object) -> None:
        del storage_arg, reservations

    monkeypatch.setattr(
        core_services,
        "_mark_reservation_collision",
        fail_collision_witness,
    )
    monkeypatch.setattr(
        core_services,
        "_cleanup_unowned",
        lose_immediate_compensation,
    )
    with pytest.raises(StorageCollision):
        create_complete_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            payloads=(payload(Artifact.Kind.HTML, "dashboard.html", b"new bytes"),),
            storage=storage,
        )

    reservation = StorageReservation.objects.get()
    assert reservation.storage_state == StorageReservation.StorageState.RESERVED
    result = cleanup_expired_reservations(
        storage,
        now=timezone.now() + timedelta(days=2),
    )

    reservation.refresh_from_db()
    assert result == core_services.CleanupResult(removed=0, retained=1)
    assert reservation.cleanup_required is True
    with storage.open(key) as stream:
        assert stream.read() == b"pre-existing bytes"


def test_commit_response_ambiguity_never_deletes_committed_artifacts(
    dashboard: Dashboard,
    owner: User,
    storage: FilesystemArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = core_services._commit_revision_metadata

    def commit_then_lose_response(
        *,
        dashboard_id: uuid.UUID,
        created_by_id: uuid.UUID,
        prepared: Sequence[core_services._PreparedArtifact],
    ) -> Revision:
        original_commit(
            dashboard_id=dashboard_id,
            created_by_id=created_by_id,
            prepared=prepared,
        )
        raise DatabaseError("simulated lost commit response")

    monkeypatch.setattr(
        core_services,
        "_commit_revision_metadata",
        commit_then_lose_response,
    )
    with pytest.raises(DatabaseError, match="lost commit response"):
        create_revision(dashboard, owner, storage)

    revision = Revision.objects.get()
    artifact = revision.artifacts.get(kind=Artifact.Kind.HTML)
    assert StorageReservation.objects.count() == 0
    with storage.open(StorageKey(artifact.storage_key)) as stream:
        assert stream.read() == b"<html>safe bytes</html>"


def test_reservation_receipt_must_match_its_key() -> None:
    reserved_key = StorageKey.generate()
    reservation = StorageReservation.objects.create(
        storage_key=reserved_key.value,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    other_key = StorageKey.generate()
    receipt = StoredArtifact(key=other_key, byte_size=0, sha256=hashlib.sha256(b"").hexdigest())

    with pytest.raises(RevisionCreationError, match="did not match"):
        core_services._mark_reservation_verified(reservation.id, receipt)


def test_storage_reservation_identity_and_verified_receipt_are_one_way() -> None:
    key = StorageKey.generate()
    digest = hashlib.sha256(b"verified").hexdigest()
    reservation = StorageReservation.objects.create(
        storage_key=key.value,
        expires_at=timezone.now() + timedelta(hours=1),
        storage_state=StorageReservation.StorageState.OWNED,
        verified_size=8,
        verified_sha256=digest,
    )

    reservation.storage_key = StorageKey.generate().value
    with pytest.raises(ImmutableRecordError, match="identity"):
        reservation.save()
    reservation.refresh_from_db()
    reservation.verified_size = 9
    with pytest.raises(ImmutableRecordError, match="receipt"):
        reservation.save()

    with pytest.raises(DatabaseError), transaction.atomic():
        StorageReservation.objects.filter(id=reservation.id).update(
            storage_key=StorageKey.generate().value
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        StorageReservation.objects.filter(id=reservation.id).update(verified_size=9)
    with pytest.raises(DatabaseError), transaction.atomic():
        StorageReservation.objects.filter(id=reservation.id).update(
            storage_state=StorageReservation.StorageState.COLLISION
        )

    reservation.refresh_from_db()
    reservation.cleanup_required = True
    reservation.save(update_fields=("cleanup_required",))
    reservation.cleanup_required = False
    with pytest.raises(ImmutableRecordError, match="cannot be cleared"):
        reservation.save(update_fields=("cleanup_required",))
    with pytest.raises(DatabaseError), transaction.atomic():
        StorageReservation.objects.filter(id=reservation.id).update(cleanup_required=False)


def prepared_artifact(
    *, reservation_id: uuid.UUID, key: StorageKey, verified: bool = True
) -> core_services._PreparedArtifact:
    content = b"<html></html>"
    receipt = StoredArtifact(
        key=key,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    item = core_services._PreparedArtifact(
        reservation_id=reservation_id,
        name=normalize_logical_name("dashboard.html"),
        payload=payload(Artifact.Kind.HTML, "dashboard.html", content),
        receipt=receipt,
        media_type="text/html",
    )
    if not verified:
        assert item.receipt.byte_size > 0
    return item


def test_metadata_commit_rechecks_owner_reservation_and_key_ownership(
    dashboard: Dashboard,
    owner: User,
    viewer: User,
    storage: FilesystemArtifactStorage,
) -> None:
    with pytest.raises(RevisionCreationError, match="must own"):
        core_services._commit_revision_metadata(
            dashboard_id=dashboard.id,
            created_by_id=viewer.id,
            prepared=(),
        )

    missing_key = StorageKey.generate()
    missing = prepared_artifact(reservation_id=uuid.uuid4(), key=missing_key)
    with pytest.raises(RevisionCreationError, match="reservation is missing"):
        core_services._commit_revision_metadata(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            prepared=(missing,),
        )

    unverified_key = StorageKey.generate()
    unverified_reservation = StorageReservation.objects.create(
        storage_key=unverified_key.value,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    unverified = prepared_artifact(
        reservation_id=unverified_reservation.id, key=unverified_key, verified=False
    )
    with pytest.raises(RevisionCreationError, match="not verified"):
        core_services._commit_revision_metadata(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            prepared=(unverified,),
        )

    revision = create_revision(dashboard, owner, storage)
    artifact = revision.artifacts.get(kind=Artifact.Kind.HTML)
    owned_key = StorageKey(artifact.storage_key)
    owned_reservation = StorageReservation.objects.create(
        storage_key=owned_key.value,
        expires_at=timezone.now() + timedelta(hours=1),
        storage_state=StorageReservation.StorageState.OWNED,
        verified_size=artifact.byte_size,
        verified_sha256=artifact.sha256,
    )
    owned = core_services._PreparedArtifact(
        reservation_id=owned_reservation.id,
        name=normalize_logical_name(artifact.logical_name),
        payload=payload(Artifact.Kind.HTML, artifact.logical_name, b"<html>safe bytes</html>"),
        receipt=StoredArtifact(
            key=owned_key,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
        ),
        media_type="text/html",
    )
    with pytest.raises(RevisionCreationError, match="already owned"):
        core_services._commit_revision_metadata(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            prepared=(owned,),
        )


def test_failure_cleanup_retains_database_ownership_when_checks_or_delete_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = StorageKey.generate()
    reservation = StorageReservation.objects.create(
        storage_key=key.value,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    storage = FilesystemArtifactStorage(tmp_path / "private")

    monkeypatch.setattr(
        Artifact.objects,
        "filter",
        lambda *args, **kwargs: (_ for _ in ()).throw(DatabaseError("database unavailable")),
    )
    candidate = core_services._CleanupCandidate(reservation.id, key, owns_bytes=True)
    core_services._cleanup_unowned(storage, (candidate,))
    assert StorageReservation.objects.filter(id=reservation.id).exists()
    monkeypatch.undo()

    failing = DeleteFailingStorage(tmp_path / "private")
    core_services._cleanup_unowned(failing, (candidate,))
    reservation.refresh_from_db()
    assert reservation.cleanup_required is True


class DeleteFailingStorage(FilesystemArtifactStorage):
    def delete(self, key: StorageKey) -> None:
        raise ArtifactStorageError("simulated cleanup failure")


def test_expired_cleanup_is_idempotent_and_retains_failed_work(tmp_path: Path) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    key = storage.generate_key()
    receipt = storage.write(key, (b"orphan",))
    reservation = StorageReservation.objects.create(
        storage_key=key.value,
        expires_at=timezone.now() - timedelta(minutes=1),
        storage_state=StorageReservation.StorageState.OWNED,
        verified_size=receipt.byte_size,
        verified_sha256=receipt.sha256,
    )

    failing = DeleteFailingStorage(tmp_path / "private")
    first = cleanup_expired_reservations(failing)
    reservation.refresh_from_db()
    assert first.retained == 1
    assert reservation.cleanup_required is True

    second = cleanup_expired_reservations(storage)
    third = cleanup_expired_reservations(storage)
    assert second.removed == 1
    assert third == core_services.CleanupResult(removed=0, retained=0)
    with pytest.raises(FileNotFoundError):
        with storage.open(key):
            pass


def test_expired_cleanup_retains_reservation_when_path_inspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FilesystemArtifactStorage(tmp_path / "private")
    key = storage.generate_key()
    reservation = StorageReservation.objects.create(
        storage_key=key.value,
        expires_at=timezone.now() - timedelta(minutes=1),
        storage_state=StorageReservation.StorageState.OWNED,
        verified_size=0,
        verified_sha256=hashlib.sha256(b"").hexdigest(),
    )
    blocked = storage._root / "v1"
    original_lstat = Path.lstat

    def deny_inspection(path: Path) -> os.stat_result:
        if path == blocked:
            raise PermissionError("sensitive path")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny_inspection)
    result = cleanup_expired_reservations(storage)

    reservation.refresh_from_db()
    assert result == core_services.CleanupResult(removed=0, retained=1)
    assert reservation.cleanup_required is True


def test_cleanup_management_command_is_bounded_and_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "private"
    storage = FilesystemArtifactStorage(root)
    key = storage.generate_key()
    receipt = storage.write(key, (b"orphan",))
    StorageReservation.objects.create(
        storage_key=key.value,
        expires_at=timezone.now() - timedelta(minutes=1),
        storage_state=StorageReservation.StorageState.OWNED,
        verified_size=receipt.byte_size,
        verified_sha256=receipt.sha256,
    )

    with override_settings(AGORA_ARTIFACT_ROOT=root):
        call_command("cleanup_artifact_reservations", limit=1)
        with pytest.raises(CommandError, match="positive"):
            call_command("cleanup_artifact_reservations", limit=0)

    output = capsys.readouterr().out
    assert "removed=1 retained=0" in output
    assert str(root) not in output


def test_cleanup_never_deletes_an_artifact_owned_key(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    revision = create_revision(dashboard, owner, storage)
    artifact = revision.artifacts.get(kind=Artifact.Kind.HTML)
    StorageReservation.objects.create(
        storage_key=artifact.storage_key,
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    result = cleanup_expired_reservations(storage)

    assert result.removed == 1
    assert StorageReservation.objects.count() == 0
    with storage.open(StorageKey(artifact.storage_key)) as stream:
        assert stream.read() == b"<html>safe bytes</html>"


def test_complete_revision_and_artifact_set_are_immutable(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    revision = create_revision(dashboard, owner, storage)
    artifact = revision.artifacts.get(kind=Artifact.Kind.HTML)
    assert str(revision) == f"{dashboard.id}:1"
    assert str(artifact) == f"{revision.id}:dashboard.html"
    with pytest.raises(ImmutableRecordError, match="cannot be deleted"):
        revision.delete()
    with pytest.raises(ImmutableRecordError, match="cannot be deleted"):
        artifact.delete()
    revision.number = 9
    with pytest.raises(ImmutableRecordError):
        revision.save()
    artifact.logical_name = "changed.html"
    with pytest.raises(ImmutableRecordError):
        artifact.save()

    with pytest.raises(DatabaseError), transaction.atomic():
        Revision.objects.filter(id=revision.id).update(number=9)
    with pytest.raises(DatabaseError), transaction.atomic():
        Artifact.objects.filter(id=artifact.id).update(byte_size=0)
    with pytest.raises(DatabaseError), transaction.atomic():
        Artifact.objects.filter(id=artifact.id).delete()

    extra = Artifact(
        revision=revision,
        kind=Artifact.Kind.CSV,
        logical_name="late.csv",
        name_key="late.csv",
        storage_key=storage.generate_key().value,
        byte_size=0,
        media_type="text/csv",
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    with pytest.raises(ValidationError, match="cannot be added"):
        extra.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        Artifact.objects.bulk_create([extra])


def test_oracle_trigger_rejects_incomplete_revision(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    csv_key = storage.generate_key()
    with pytest.raises(DatabaseError, match="exactly one HTML"), transaction.atomic():
        revision = Revision.objects.create(dashboard=dashboard, number=1, created_by=owner)
        Artifact.objects.create(
            revision=revision,
            kind=Artifact.Kind.CSV,
            logical_name="only.csv",
            storage_key=csv_key.value,
            byte_size=0,
            media_type="text/csv",
            sha256=hashlib.sha256(b"").hexdigest(),
        )
        Revision.objects.filter(id=revision.id).update(artifacts_locked=True)
        dashboard.latest_revision = revision
        dashboard.save()

    assert Revision.objects.count() == 0


def test_database_rejects_foreign_latest_and_published_pointers(
    owner: User, storage: FilesystemArtifactStorage
) -> None:
    first_dashboard = Dashboard.objects.create(owner=owner, name="First")
    second_dashboard = Dashboard.objects.create(owner=owner, name="Second")
    first_revision = create_revision(first_dashboard, owner, storage)
    second_revision = create_revision(second_dashboard, owner, storage)

    with pytest.raises(IntegrityError), transaction.atomic():
        Dashboard.objects.filter(id=first_dashboard.id).update(
            state=Dashboard.State.PUBLISHED,
            published_revision=second_revision,
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        Dashboard.objects.filter(id=first_dashboard.id).update(latest_revision=second_revision)

    first_dashboard.refresh_from_db()
    assert first_dashboard.latest_revision_id == first_revision.id
    assert first_dashboard.published_revision_id is None

    first_dashboard.published_revision = second_revision
    with pytest.raises(ValidationError, match="published revision must belong"):
        first_dashboard.full_clean()
    first_dashboard.published_revision = None
    first_dashboard.latest_revision = second_revision
    with pytest.raises(ValidationError, match="latest revision must belong"):
        first_dashboard.full_clean()


def test_revision_model_rejects_a_non_owner_creator(dashboard: Dashboard, viewer: User) -> None:
    revision = Revision(dashboard=dashboard, number=1, created_by=viewer)

    with pytest.raises(ValidationError, match="creator must own"):
        revision.full_clean()


def test_artifact_model_rejects_invalid_or_noncanonical_names(
    dashboard: Dashboard, owner: User, storage: FilesystemArtifactStorage
) -> None:
    revision = create_revision(dashboard, owner, storage)
    invalid = Artifact(
        revision=revision,
        kind=Artifact.Kind.CSV,
        logical_name="../bad.csv",
        storage_key=StorageKey.generate().value,
        byte_size=0,
        media_type="text/csv",
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    with pytest.raises(ValidationError, match="path separators"):
        invalid.full_clean()
    noncanonical = Artifact(
        revision=revision,
        kind=Artifact.Kind.CSV,
        logical_name="Report.csv",
        name_key="wrong",
        storage_key=StorageKey.generate().value,
        byte_size=0,
        media_type="text/csv",
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    with pytest.raises(ValidationError, match="comparison key"):
        noncanonical.clean()


def test_database_rejects_non_nfc_artifact_names_from_bulk_writes(
    dashboard: Dashboard, owner: User
) -> None:
    empty_digest = hashlib.sha256(b"").hexdigest()
    with pytest.raises(DatabaseError, match="canonical"), transaction.atomic():
        revision = Revision.objects.create(dashboard=dashboard, number=1, created_by=owner)
        Artifact.objects.bulk_create(
            [
                Artifact(
                    revision=revision,
                    kind=Artifact.Kind.HTML,
                    logical_name="dashboard.html",
                    name_key="dashboard.html",
                    storage_key=StorageKey.generate().value,
                    byte_size=0,
                    media_type="text/html",
                    sha256=empty_digest,
                ),
                Artifact(
                    revision=revision,
                    kind=Artifact.Kind.CSV,
                    logical_name="café.csv",
                    name_key="fabricated-one",
                    storage_key=StorageKey.generate().value,
                    byte_size=0,
                    media_type="text/csv",
                    sha256=empty_digest,
                ),
                Artifact(
                    revision=revision,
                    kind=Artifact.Kind.CSV,
                    logical_name="cafe\u0301.csv",
                    name_key="fabricated-two",
                    storage_key=StorageKey.generate().value,
                    byte_size=0,
                    media_type="text/csv",
                    sha256=empty_digest,
                ),
            ]
        )


def test_model_string_forms_do_not_disclose_storage_keys(
    dashboard: Dashboard, owner: User, viewer: User
) -> None:
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    event = AuditEvent.objects.create(event_type="grant.created", actor=owner, dashboard=dashboard)
    reservation = StorageReservation.objects.create(
        storage_key=StorageKey.generate().value,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    assert str(grant) == f"{dashboard.id}:{viewer.id}"
    assert str(event) == f"{event.id}:grant.created"
    assert str(reservation) == str(reservation.id)


def test_grant_relationship_and_wrong_revoker_are_model_guarded(
    dashboard: Dashboard, owner: User, viewer: User
) -> None:
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    replacement = User.objects.create_user("REPLACEMENT")
    grant.viewer = replacement
    with pytest.raises(ImmutableRecordError, match="relationship fields"):
        grant.save()

    wrong_revoker = ViewerGrant(
        dashboard=dashboard,
        viewer=replacement,
        created_by=owner,
        revoked_at=timezone.now(),
        revoked_by=viewer,
    )
    with pytest.raises(ValidationError, match="only the dashboard owner can revoke"):
        wrong_revoker.full_clean()
    with pytest.raises(DatabaseError), transaction.atomic():
        ViewerGrant.objects.bulk_create([wrong_revoker])


def test_viewer_grants_are_unique_owner_created_and_retained(
    dashboard: Dashboard, owner: User, viewer: User
) -> None:
    with pytest.raises(ValidationError, match="cannot grant themselves"):
        ViewerGrant.objects.create(dashboard=dashboard, viewer=owner, created_by=owner)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    with pytest.raises(IntegrityError), transaction.atomic():
        ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    with pytest.raises(ImmutableRecordError):
        grant.delete()

    with pytest.raises(IntegrityError), transaction.atomic():
        ViewerGrant.objects.bulk_create(
            [ViewerGrant(dashboard=dashboard, viewer=viewer, created_by=owner)]
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        ViewerGrant.objects.bulk_create(
            [ViewerGrant(dashboard=dashboard, viewer=owner, created_by=owner)]
        )


def test_grant_creator_and_revoker_must_be_owner(
    dashboard: Dashboard, owner: User, viewer: User
) -> None:
    unrelated = User.objects.create_user("UNRELATED")
    with pytest.raises(ValidationError, match="only the dashboard owner"):
        ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=unrelated)

    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save()
    original_revoked_at = grant.revoked_at
    grant.revoked_at = timezone.now() + timedelta(seconds=1)
    with pytest.raises(ImmutableRecordError, match="revocation is immutable"):
        grant.save()
    grant.refresh_from_db()
    assert grant.revoked_at == original_revoked_at
    grant.revoked_at = None
    grant.revoked_by = None
    with pytest.raises(ImmutableRecordError, match="revocation is immutable"):
        grant.save()
    grant.refresh_from_db()
    assert grant.revoked_at == original_revoked_at


def test_audit_events_are_json_objects_and_append_only(dashboard: Dashboard, owner: User) -> None:
    event = AuditEvent.objects.create(
        event_type="dashboard.created", actor=owner, dashboard=dashboard, metadata={"safe": True}
    )
    event.metadata = {"changed": True}
    with pytest.raises(ImmutableRecordError):
        event.save()
    with pytest.raises(ImmutableRecordError):
        event.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEvent.objects.filter(id=event.id).update(event_type="dashboard.changed")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEvent.objects.filter(id=event.id).delete()
    with pytest.raises(DatabaseError, match="JSON object"), transaction.atomic():
        AuditEvent.objects.bulk_create(
            [AuditEvent(event_type="system.invalid", metadata=cast(dict[str, object], []))]
        )
    invalid = AuditEvent(event_type="system.invalid", metadata=cast(dict[str, object], []))
    with pytest.raises(ValidationError, match="JSON object"):
        invalid.full_clean()


def test_critical_oracle_constraints_indexes_and_triggers_are_installed() -> None:
    expected_tables = {
        "TB_TA_AGORA_ARTIFACT",
        "TB_TA_AGORA_AUDIT_EVENT",
        "TB_TA_AGORA_AUTH_GROUP",
        "TB_TA_AGORA_AUTH_GROUP_PERMISSIONS",
        "TB_TA_AGORA_AUTH_PERMISSION",
        "TB_TA_AGORA_DASHBOARD",
        "TB_TA_AGORA_DJANGO_CONTENT_TYPE",
        "TB_TA_AGORA_DJANGO_MIGRATIONS",
        "TB_TA_AGORA_DJANGO_SESSION",
        "TB_TA_AGORA_LOGIN_THROTTLE",
        "TB_TA_AGORA_RENDER_AUTHORIZATION",
        "TB_TA_AGORA_REVISION",
        "TB_TA_AGORA_STORAGE_RESERVATION",
        "TB_TA_AGORA_USER",
        "TB_TA_AGORA_VIEWER_GRANT",
    }
    replaced_tables = {
        "AUTH_GROUP",
        "AUTH_GROUP_PERMISSIONS",
        "AUTH_PERMISSION",
        "DJANGO_CONTENT_TYPE",
        "DJANGO_MIGRATIONS",
        "DJANGO_SESSION",
        "TB_TA_ARTIFACT",
        "TB_TA_AUDIT_EVENT",
        "TB_TA_DASHBOARD",
        "TB_TA_LOGIN_THROTTLE",
        "TB_TA_RENDER_AUTHORIZATION",
        "TB_TA_REVISION",
        "TB_TA_STORAGE_RESERVATION",
        "TB_TA_USER",
        "TB_TA_VIEWER_GRANT",
    }
    logical_constraint_names = {
        "agora_artifact_revision_name_unique",
        "agora_dashboard_publication_history",
        "agora_reservation_state_receipt_match",
        "agora_reservation_valid_storage_state",
    }
    retired_lifetime_grant_constraint = connection.ops.quote_name(
        "agora_grant_dashboard_viewer_unique"
    ).strip('"')
    retired_owner_equality_constraints = {
        "AGORA_REV_CREATOR_OWNER_FK",
        "AGORA_GRANT_CREATOR_OWNER_FK",
        "AGORA_GRANT_REVOKER_OWNER_FK",
    }
    expected_constraints = {
        "AGORA_DASH_LATEST_OWNED_FK",
        "AGORA_DASH_PUBLISHED_OWNED_FK",
        "AGORA_ARTIFACT_STORAGE_SHARDS",
        "AGORA_RESERVATION_STORAGE_SHARDS",
        *(connection.ops.quote_name(name).strip('"') for name in logical_constraint_names),
    }
    expected_indexes = {
        "AGORA_ARTIFACT_ONE_HTML_IDX",
        "AGORA_GRANT_ACTIVE_UQ_IDX",
        "AGORA_GRANT_DASH_ACTIVE_IDX",
        "AGORA_GRANT_SCOPE_ACTIVE_IDX",
        "AGORA_GRANT_VIEWER_ACTIVE_IDX",
    }
    expected_triggers = {
        "AGORA_REVISION_COMPLETE_GUARD",
        "AGORA_DASHBOARD_LATEST_GUARD",
        "AGORA_REVISION_IMMUT_GUARD",
        "AGORA_ARTIFACT_IMMUT_GUARD",
        "AGORA_ARTIFACT_INSERT_GUARD",
        "AGORA_AUDIT_APPEND_GUARD",
        "AGORA_DASHBOARD_GUARD",
        "AGORA_REVISION_AUTH_GUARD",
        "AGORA_RESERVATION_MUT_GUARD",
        "AGORA_GRANT_IMMUT_GUARD",
        "AGORA_USER_AUTH_VERSION_GUARD",
        "AGORA_USER_RETENTION_GUARD",
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT table_name FROM user_tables")
        tables = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT constraint_name FROM user_constraints WHERE constraint_name LIKE 'AGORA%'"
        )
        installed_constraints = {row[0] for row in cursor.fetchall()}
        constraints = installed_constraints & expected_constraints
        cursor.execute(
            "SELECT index_name, uniqueness, table_name "
            "FROM user_indexes WHERE index_name LIKE 'AGORA%'"
        )
        index_definitions = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        indexes = index_definitions.keys() & expected_indexes
        cursor.execute(
            "SELECT index_name, column_position, column_name "
            "FROM user_ind_columns "
            "WHERE index_name IN ("
            "'AGORA_GRANT_DASH_ACTIVE_IDX', "
            "'AGORA_GRANT_SCOPE_ACTIVE_IDX', "
            "'AGORA_GRANT_VIEWER_ACTIVE_IDX'"
            ") ORDER BY index_name, column_position"
        )
        lookup_index_columns: dict[str, list[str]] = {}
        for index_name, _position, column_name in cursor.fetchall():
            lookup_index_columns.setdefault(index_name, []).append(column_name)
        cursor.execute(
            "SELECT column_expression FROM user_ind_expressions "
            "WHERE index_name = 'AGORA_GRANT_ACTIVE_UQ_IDX' "
            "ORDER BY column_position"
        )
        active_grant_expressions = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT trigger_name FROM user_triggers WHERE trigger_name LIKE 'AGORA%'")
        triggers = {row[0] for row in cursor.fetchall()} & expected_triggers
        cursor.execute("SELECT name FROM user_errors WHERE name LIKE 'AGORA%'")
        compile_errors = cursor.fetchall()

    assert expected_tables <= tables
    assert replaced_tables.isdisjoint(tables)
    assert not any(name.startswith("PERSISTENCE_") for name in tables)
    assert constraints == expected_constraints
    assert retired_lifetime_grant_constraint not in installed_constraints
    assert retired_owner_equality_constraints.isdisjoint(installed_constraints)
    assert indexes == expected_indexes
    assert index_definitions["AGORA_GRANT_ACTIVE_UQ_IDX"] == (
        "UNIQUE",
        "TB_TA_AGORA_VIEWER_GRANT",
    )
    for lookup_index in expected_indexes - {
        "AGORA_ARTIFACT_ONE_HTML_IDX",
        "AGORA_GRANT_ACTIVE_UQ_IDX",
    }:
        assert index_definitions[lookup_index] == (
            "NONUNIQUE",
            "TB_TA_AGORA_VIEWER_GRANT",
        )
    assert lookup_index_columns["AGORA_GRANT_DASH_ACTIVE_IDX"] == [
        "DASHBOARD_ID",
        "REVOKED_AT",
    ]
    assert lookup_index_columns["AGORA_GRANT_SCOPE_ACTIVE_IDX"] == [
        "DASHBOARD_ID",
        "VIEWER_ID",
        "REVOKED_AT",
    ]
    viewer_index_columns = lookup_index_columns["AGORA_GRANT_VIEWER_ACTIVE_IDX"]
    assert viewer_index_columns[:2] == ["VIEWER_ID", "REVOKED_AT"]
    assert len(viewer_index_columns) == 4
    assert all(column.startswith("SYS_NC") for column in viewer_index_columns[2:])
    normalized_active_grant_expressions = [
        " ".join(
            str(expression).upper().replace('"', "").replace("(", " ").replace(")", " ").split()
        )
        for expression in active_grant_expressions
    ]
    assert normalized_active_grant_expressions == [
        "CASE WHEN REVOKED_AT IS NULL THEN DASHBOARD_ID ELSE NULL END",
        "CASE WHEN REVOKED_AT IS NULL THEN VIEWER_ID ELSE NULL END",
    ]
    assert triggers == expected_triggers
    assert compile_errors == []
