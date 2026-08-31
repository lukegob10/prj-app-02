from __future__ import annotations

import hashlib
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from agora.core.access import (
    GrantRejection,
    GrantViewerRejected,
    ProjectAccessDenied,
    active_viewer_grant,
    grant_project_viewer,
    has_active_viewer_grant,
    revoke_project_viewer,
    user_can_view_published,
)
from agora.core.models import (
    AuditEvent,
    Dashboard,
    ImmutableRecordError,
    RenderAuthorization,
    Revision,
    User,
    ViewerGrant,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _user(soeid: str, *, administrator: bool = False) -> User:
    return User.objects.create_user(soeid, is_administrator=administrator)


def _published_dashboard(owner: User, *, name: str = "Published project") -> Dashboard:
    dashboard = Dashboard.objects.create(owner=owner, name=name)
    revision = Revision.objects.create(dashboard=dashboard, number=1, created_by=owner)
    dashboard.latest_revision = revision
    dashboard.published_revision = revision
    dashboard.first_published_at = timezone.now()
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.save()
    return dashboard


def test_grant_normalizes_target_and_records_an_auditable_epoch() -> None:
    owner = _user("ACCESS.OWNER")
    viewer = _user("ACCESS.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")

    grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid="\taccess.viewer\n",
    )

    assert grant.dashboard_id == dashboard.id
    assert grant.viewer_id == viewer.id
    assert grant.created_by_id == owner.id
    assert grant.revoked_at is None
    event = AuditEvent.objects.get(event_type="grant.created")
    assert event.actor_id == owner.id
    assert event.target_user_id == viewer.id
    assert event.dashboard_id == dashboard.id
    assert event.metadata == {"grant_id": str(grant.id)}
    active = active_viewer_grant(dashboard_id=dashboard.id, viewer_id=viewer.id)
    assert active is not None
    assert active.id == grant.id
    assert has_active_viewer_grant(dashboard_id=dashboard.id, viewer_id=viewer.id)


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("bad value!", GrantRejection.INVALID_SOEID),
        ("ACCESS.MISSING", GrantRejection.UNKNOWN_USER),
    ],
)
def test_grant_rejects_invalid_or_unknown_targets(target: str, reason: GrantRejection) -> None:
    owner = _user("ACCESS.REJECT.OWNER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")

    with pytest.raises(GrantViewerRejected) as raised:
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid=target,
        )

    assert raised.value.reason is reason
    assert not AuditEvent.objects.exists()


def test_grant_rejects_disabled_self_and_active_duplicate_targets() -> None:
    owner = _user("ACCESS.CHECK.OWNER")
    disabled = _user("ACCESS.DISABLED")
    disabled.is_active = False
    disabled.save(update_fields=("is_active", "updated_at"))
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")

    with pytest.raises(GrantViewerRejected) as raised:
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid=disabled.soeid,
        )
    assert raised.value.reason is GrantRejection.DISABLED_USER

    with pytest.raises(GrantViewerRejected) as raised:
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid=owner.soeid,
        )
    assert raised.value.reason is GrantRejection.SELF_GRANT

    viewer = _user("ACCESS.DUPLICATE")
    grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    with pytest.raises(GrantViewerRejected) as raised:
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid=viewer.soeid,
        )
    assert raised.value.reason is GrantRejection.ALREADY_GRANTED
    assert AuditEvent.objects.filter(event_type="grant.created").count() == 1


def test_grant_maps_a_lost_unique_index_race_after_rolling_back_its_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("ACCESS.RACE.OWNER")
    viewer = _user("ACCESS.RACE.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    active_checks = iter((False, True))

    def simulated_active_grant(**_kwargs: UUID) -> bool:
        return next(active_checks)

    def lose_insert_race(**_kwargs: object) -> ViewerGrant:
        raise IntegrityError("simulated active-grant unique-index race")

    monkeypatch.setattr(
        "agora.core.access.has_active_viewer_grant",
        simulated_active_grant,
    )
    monkeypatch.setattr(ViewerGrant.objects, "create", lose_insert_race)

    with pytest.raises(GrantViewerRejected) as raised:
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid=viewer.soeid,
        )

    assert raised.value.reason is GrantRejection.ALREADY_GRANTED
    # The inner savepoint absorbed the failed insert, so the durable outer
    # transaction could finish normally instead of becoming unusable.
    assert User.objects.filter(id=viewer.id, is_active=True).exists()
    assert not AuditEvent.objects.exists()


def test_revoke_is_one_way_idempotent_and_regrant_opens_a_new_epoch() -> None:
    owner = _user("ACCESS.REVOKE.OWNER")
    viewer = _user("ACCESS.REVOKE.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    first = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )

    revoked = revoke_project_viewer(
        dashboard_id=dashboard.id,
        grant_id=first.id,
        actor_id=owner.id,
    )
    retried = revoke_project_viewer(
        dashboard_id=dashboard.id,
        grant_id=first.id,
        actor_id=owner.id,
    )

    assert revoked.id == first.id
    assert retried.id == first.id
    assert retried.revoked_at is not None
    assert retried.revoked_by_id == owner.id
    assert not has_active_viewer_grant(dashboard_id=dashboard.id, viewer_id=viewer.id)
    assert AuditEvent.objects.filter(event_type="grant.revoked").count() == 1

    second = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    assert second.id != first.id
    assert second.revoked_at is None
    assert ViewerGrant.objects.filter(dashboard=dashboard, viewer=viewer).count() == 2
    active = active_viewer_grant(dashboard_id=dashboard.id, viewer_id=viewer.id)
    assert active is not None
    assert active.id == second.id


def test_revoke_rejects_missing_foreign_and_non_owner_callers() -> None:
    owner = _user("ACCESS.AUTH.OWNER")
    viewer = _user("ACCESS.AUTH.VIEWER")
    outsider = _user("ACCESS.AUTH.OUTSIDER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )

    with pytest.raises(ProjectAccessDenied):
        revoke_project_viewer(
            dashboard_id=dashboard.id,
            grant_id=grant.id,
            actor_id=outsider.id,
        )
    with pytest.raises(ProjectAccessDenied):
        revoke_project_viewer(
            dashboard_id=uuid4(),
            grant_id=grant.id,
            actor_id=owner.id,
        )
    with pytest.raises(ProjectAccessDenied):
        revoke_project_viewer(
            dashboard_id=dashboard.id,
            grant_id=uuid4(),
            actor_id=owner.id,
        )
    assert grant.revoked_at is None


def test_grant_management_requires_an_active_owner_and_does_not_enumerate() -> None:
    owner = _user("ACCESS.OWNER.ONLY")
    outsider = _user("ACCESS.OUTSIDER.ONLY")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    owner.is_active = False
    owner.save(update_fields=("is_active", "updated_at"))

    cases = ((owner.id, dashboard.id), (outsider.id, dashboard.id), (owner.id, uuid4()))
    for actor_id, project_id in cases:
        with pytest.raises(ProjectAccessDenied) as raised:
            grant_project_viewer(
                dashboard_id=project_id,
                actor_id=actor_id,
                target_soeid="ACCESS.TARGET",
            )
        assert str(raised.value) == "project access is not available"


def test_archived_projects_only_allow_existing_epoch_revocation() -> None:
    owner = _user("ACCESS.ARCHIVE.OWNER")
    viewer = _user("ACCESS.ARCHIVE.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Archived project")
    grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    dashboard.state = Dashboard.State.ARCHIVED
    dashboard.save()

    with pytest.raises(ProjectAccessDenied):
        grant_project_viewer(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            target_soeid="ACCESS.NEW.VIEWER",
        )
    revoked = revoke_project_viewer(
        dashboard_id=dashboard.id,
        grant_id=grant.id,
        actor_id=owner.id,
    )
    assert revoked.revoked_at is not None


def test_deleted_projects_fail_closed_for_all_grant_management() -> None:
    owner = _user("ACCESS.DELETE.OWNER")
    viewer = _user("ACCESS.DELETE.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Deleted project")
    grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    dashboard.state = Dashboard.State.DELETED
    dashboard.save()

    with pytest.raises(ProjectAccessDenied):
        revoke_project_viewer(
            dashboard_id=dashboard.id,
            grant_id=grant.id,
            actor_id=owner.id,
        )


def test_published_policy_is_project_scoped_and_fail_closed() -> None:
    owner = _user("ACCESS.POLICY.OWNER")
    viewer = _user("ACCESS.POLICY.VIEWER")
    administrator = _user("ACCESS.POLICY.ADMIN", administrator=True)
    unrelated = _user("ACCESS.POLICY.UNRELATED")
    dashboard = _published_dashboard(owner)

    assert user_can_view_published(dashboard=dashboard, viewer=owner)
    assert not user_can_view_published(dashboard=dashboard, viewer=administrator)
    assert not user_can_view_published(dashboard=dashboard, viewer=unrelated)
    assert not user_can_view_published(dashboard=dashboard, viewer=viewer)

    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    assert user_can_view_published(dashboard=dashboard, viewer=viewer)
    assert user_can_view_published(dashboard=dashboard, viewer=viewer, viewer_grant=grant)

    other_dashboard = _published_dashboard(owner, name="Another project")
    assert not user_can_view_published(
        dashboard=other_dashboard,
        viewer=viewer,
        viewer_grant=grant,
    )
    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save(update_fields=("revoked_at", "revoked_by"))
    assert not user_can_view_published(dashboard=dashboard, viewer=viewer, viewer_grant=grant)

    viewer.is_active = False
    assert not user_can_view_published(dashboard=dashboard, viewer=viewer, viewer_grant=grant)

    draft = Dashboard.objects.create(owner=owner, name="Still private")
    assert not user_can_view_published(dashboard=draft, viewer=owner)


def test_revoked_epochs_cannot_be_reopened_through_model_or_bulk_update() -> None:
    owner = _user("ACCESS.IMMUT.OWNER")
    viewer = _user("ACCESS.IMMUT.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save(update_fields=("revoked_at", "revoked_by"))
    original_revoked_at = grant.revoked_at

    grant.revoked_at = None
    grant.revoked_by = None
    with pytest.raises(ImmutableRecordError, match="revocation is immutable"):
        grant.save()
    grant.refresh_from_db()
    assert grant.revoked_at == original_revoked_at

    if connection.vendor == "oracle":
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                ViewerGrant.objects.filter(id=grant.id).update(
                    revoked_at=None,
                    revoked_by=None,
                )


def test_render_authorization_binds_the_exact_active_epoch() -> None:
    owner = _user("ACCESS.RENDER.OWNER")
    viewer = _user("ACCESS.RENDER.VIEWER")
    dashboard = Dashboard.objects.create(owner=owner, name="Private project")
    revision = Revision.objects.create(dashboard=dashboard, number=1, created_by=owner)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    authorization = RenderAuthorization.objects.create(
        token_digest=hashlib.sha256(uuid4().bytes).hexdigest(),
        audience=RenderAuthorization.Audience.VIEWER,
        viewer=viewer,
        viewer_auth_version=viewer.auth_version,
        dashboard=dashboard,
        revision=revision,
        viewer_grant=grant,
        publication_version=1,
        expires_at=timezone.now(),
    )
    assert authorization.viewer_grant_id == grant.id

    authorization.viewer_grant = None
    with pytest.raises(ImmutableRecordError, match="scope is immutable"):
        authorization.save()


def test_epoch_migration_uses_prefixed_oracle_active_index() -> None:
    migration = import_module("agora.core.migrations.0011_project_viewer_epochs")
    assert migration.Migration.dependencies == [
        ("persistence", "0010_apply_agora_project_table_prefix")
    ]
    assert "TB_TA_AGORA_VIEWER_GRANT" in migration.ACTIVE_GRANT_INDEX_SQL
    assert "CASE WHEN revoked_at IS NULL THEN dashboard_id" in migration.ACTIVE_GRANT_INDEX_SQL
    assert "CASE WHEN revoked_at IS NULL THEN viewer_id" in migration.ACTIVE_GRANT_INDEX_SQL
    assert "AGORA_GRANT_IMMUT_GUARD" in migration.IMMUTABLE_EPOCH_TRIGGER_SQL
    assert "NEW.revoked_at IS NULL" in migration.IMMUTABLE_EPOCH_TRIGGER_SQL
