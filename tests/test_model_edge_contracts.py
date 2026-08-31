"""Focused coverage for defensive model invariants and retained-record edges."""

from __future__ import annotations

from copy import copy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from agora.core.models import (
    AccessRequest,
    AnalyticsPipelineCheckpoint,
    Artifact,
    AuthorizedOpen,
    Dashboard,
    DashboardFavorite,
    DashboardOpenDaily,
    DashboardOpenSnapshot,
    DashboardOwnershipTransfer,
    DashboardTag,
    DashboardViewerOpenSummary,
    DashboardViewerState,
    ImmutableRecordError,
    LoginThrottle,
    RenderAuthorization,
    Revision,
    StorageReservation,
    User,
    ViewerGrant,
)
from agora.core.storage import StorageKey


def _user(label: str, *, active: bool = True) -> User:
    return User(id=uuid4(), soeid=label, is_active=active)


def _dashboard(
    owner: User,
    *,
    state: str = Dashboard.State.DRAFT,
    publication_version: int = 0,
) -> Dashboard:
    published_at = timezone.now() if publication_version else None
    return Dashboard(
        id=uuid4(),
        owner=owner,
        name="Edge contract",
        state=state,
        first_published_at=published_at,
        publication_version=publication_version,
        last_published_at=published_at,
    )


def _published_dashboard(owner: User) -> Dashboard:
    return _dashboard(owner, state=Dashboard.State.PUBLISHED, publication_version=1)


def _revision(dashboard: Dashboard, creator: User | None = None) -> Revision:
    return Revision(
        id=uuid4(),
        dashboard=dashboard,
        number=1,
        created_by=creator or dashboard.owner,
    )


def _authorization(
    dashboard: Dashboard,
    viewer: User,
    *,
    audience: str = RenderAuthorization.Audience.VIEWER,
    grant: ViewerGrant | None = None,
    epoch: DashboardOwnershipTransfer | None = None,
) -> RenderAuthorization:
    created_at = timezone.now()
    return RenderAuthorization(
        id=uuid4(),
        token_digest="0" * 64,
        audience=audience,
        viewer=viewer,
        viewer_auth_version=viewer.auth_version,
        dashboard=dashboard,
        revision=_revision(dashboard),
        viewer_grant=grant,
        owner_transfer_epoch=epoch,
        publication_version=(1 if audience == RenderAuthorization.Audience.VIEWER else None),
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
    )


def test_user_and_artifact_validation_reject_malformed_boundary_values() -> None:
    with pytest.raises(ValidationError, match="SOEID"):
        _user("not a valid soeid").clean()

    owner = _user("ARTIFACT.OWNER")
    revision = _revision(_dashboard(owner))
    artifact = Artifact(
        revision=revision,
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        name_key="dashboard.html",
        storage_key=str(StorageKey.generate()),
        byte_size=1,
        media_type="text/plain",
        sha256="0" * 64,
    )
    with pytest.raises(ValidationError, match="media type is not allowed"):
        artifact.clean()


def test_retained_models_and_simple_string_forms() -> None:
    owner = _user("EDGE.OWNER")
    viewer = _user("EDGE.VIEWER")
    dashboard = _dashboard(owner)
    revision = _revision(dashboard)
    transfer = DashboardOwnershipTransfer(
        id=uuid4(),
        dashboard=dashboard,
        from_owner=owner,
        to_owner=viewer,
    )
    tag = DashboardTag(id=1, dashboard=dashboard, label="Risk", key="risk", slot=1)
    favorite = DashboardFavorite(id=1, user=viewer, dashboard=dashboard)
    viewed_at = timezone.now()
    viewer_state = DashboardViewerState(
        id=1,
        user=viewer,
        dashboard=dashboard,
        last_viewed_at=viewed_at,
    )
    request = AccessRequest(id=1, dashboard=dashboard, requester=viewer)
    opened = AuthorizedOpen(id=1, dashboard=dashboard)
    daily = DashboardOpenDaily(id=1, dashboard=dashboard, day=viewed_at.date())
    summary = DashboardViewerOpenSummary(
        id=1,
        dashboard=dashboard,
        viewer=viewer,
        first_opened_at=viewed_at,
        last_opened_at=viewed_at,
    )
    snapshot = DashboardOpenSnapshot(
        id=1,
        dashboard=dashboard,
        last_opened_at=viewed_at,
    )
    checkpoint = AnalyticsPipelineCheckpoint(pipeline_key="authorized_opens_v1")

    assert str(LoginThrottle(id=7)) == "login-throttle:7"
    assert str(transfer) == f"{dashboard.id}:{transfer.id}"
    assert str(tag) == "Risk"
    assert str(favorite) == f"{viewer.id}:{dashboard.id}"
    assert str(viewer_state) == f"{viewer.id}:{dashboard.id}"
    assert str(request) == f"{dashboard.id}:{viewer.id}:pending"
    assert str(opened) == f"1:{dashboard.id}"
    assert str(daily) == f"{dashboard.id}:{viewed_at.date()}"
    assert str(summary) == f"{dashboard.id}:{viewer.id}"
    assert str(snapshot) == str(dashboard.id)
    assert str(checkpoint) == "authorized_opens_v1"

    transfer._state.adding = False
    with pytest.raises(ImmutableRecordError, match="history is immutable"):
        transfer.save()
    with pytest.raises(ImmutableRecordError, match="history is retained"):
        transfer.delete()
    with pytest.raises(ImmutableRecordError, match="rows are retained"):
        request.delete()
    with pytest.raises(ImmutableRecordError, match="bounded retention"):
        opened.delete()
    with pytest.raises(ImmutableRecordError, match="checkpoint is retained"):
        checkpoint.delete()
    with pytest.raises(ImmutableRecordError, match="revisions are retained"):
        revision.delete()


def test_dashboard_validation_and_lifecycle_edge_contracts() -> None:
    owner = _user("DASH.EDGE.OWNER")
    invalid_new = _dashboard(owner, state=Dashboard.State.PUBLISHED)
    with pytest.raises(ValidationError, match="private drafts"):
        invalid_new.save()

    dashboard = _dashboard(owner)
    dashboard.freshness_interval_seconds = 60
    with pytest.raises(ValidationError, match="must be set together"):
        dashboard.clean()

    confirmed_at = timezone.now()
    dashboard.freshness_interval_seconds = 0
    dashboard.freshness_confirmed_at = confirmed_at
    dashboard.stale_after = confirmed_at
    with pytest.raises(ValidationError, match="between 1 second and 1 year"):
        dashboard.clean()

    dashboard.freshness_interval_seconds = 60
    with pytest.raises(ValidationError, match="confirmation plus interval"):
        dashboard.clean()

    dashboard = _dashboard(owner)
    dashboard.publication_version = 1
    with pytest.raises(ValidationError, match="version zero"):
        dashboard.clean()

    dashboard = _dashboard(owner, state=Dashboard.State.PUBLISHED)
    with pytest.raises(ValidationError, match="must have a publication version"):
        dashboard.clean()

    deleted = _dashboard(owner, state=Dashboard.State.DELETED)
    with pytest.raises(ImmutableRecordError, match="terminal tombstones"):
        copy(deleted)._validate_lifecycle_transition(deleted)

    published = _published_dashboard(owner)
    published.published_revision_id = uuid4()
    changed_first = copy(published)
    assert published.first_published_at is not None
    changed_first.first_published_at = published.first_published_at + timedelta(seconds=1)
    with pytest.raises(ImmutableRecordError, match="first publication time"):
        changed_first._validate_lifecycle_transition(published)

    draft = _dashboard(owner)
    invented_history = copy(draft)
    invented_history.state = Dashboard.State.ARCHIVED
    invented_history.first_published_at = timezone.now()
    with pytest.raises(ValidationError, match="history begins"):
        invented_history._validate_lifecycle_transition(draft)

    lower_version = copy(published)
    lower_version.publication_version = 0
    with pytest.raises(ImmutableRecordError, match="cannot decrease"):
        lower_version._validate_lifecycle_transition(published)

    skipped_version = copy(draft)
    skipped_version.publication_version = 2
    with pytest.raises(ValidationError, match="one version at a time"):
        skipped_version._validate_lifecycle_transition(draft)

    incomplete_publication = copy(draft)
    incomplete_publication.publication_version = 1
    with pytest.raises(ValidationError, match="requires a published revision"):
        incomplete_publication._validate_lifecycle_transition(draft)

    republished = copy(published)
    republished.publication_version = 2
    republished.last_published_at = None
    with pytest.raises(ValidationError, match="time must advance"):
        republished._validate_lifecycle_transition(published)

    changed_time = copy(published)
    assert published.last_published_at is not None
    changed_time.last_published_at = published.last_published_at + timedelta(seconds=1)
    with pytest.raises(ImmutableRecordError, match="time changes only"):
        changed_time._validate_lifecycle_transition(published)

    changed_note = copy(published)
    changed_note.publication_note = "changed"
    with pytest.raises(ImmutableRecordError, match="note changes only"):
        changed_note._validate_lifecycle_transition(published)

    changed_revision = copy(published)
    changed_revision.published_revision_id = uuid4()
    with pytest.raises(ValidationError, match="must advance publication version"):
        changed_revision._validate_lifecycle_transition(published)

    invalid_transition = copy(draft)
    invalid_transition.state = Dashboard.State.UNPUBLISHED
    with pytest.raises(ValidationError, match="transition is not allowed"):
        invalid_transition._validate_lifecycle_transition(draft)

    archived = _dashboard(owner, state=Dashboard.State.ARCHIVED)
    restored = copy(archived)
    restored.state = Dashboard.State.DRAFT
    restored.name = "Changed while archived"
    with pytest.raises(ValidationError, match="read-only"):
        restored._validate_lifecycle_transition(archived)


def test_transfer_tag_viewer_state_and_revision_edges() -> None:
    owner = _user("RELATED.OWNER")
    incoming = _user("RELATED.INCOMING")
    other = _user("RELATED.OTHER")
    dashboard = _dashboard(owner)

    wrong_source = DashboardOwnershipTransfer(
        dashboard=dashboard,
        from_owner=other,
        to_owner=incoming,
    )
    with pytest.raises(ValidationError, match="source must be"):
        wrong_source.clean()

    same_owner = DashboardOwnershipTransfer(
        dashboard=dashboard,
        from_owner=owner,
        to_owner=owner,
    )
    with pytest.raises(ValidationError, match="must be different"):
        same_owner.clean()

    disabled_incoming = _user("RELATED.DISABLED", active=False)
    disabled_target = DashboardOwnershipTransfer(
        dashboard=dashboard,
        from_owner=owner,
        to_owner=disabled_incoming,
    )
    with pytest.raises(ValidationError, match="must be active"):
        disabled_target.clean()

    broken_chain = DashboardOwnershipTransfer(
        dashboard=dashboard,
        from_owner=owner,
        to_owner=incoming,
        previous_transfer_id=uuid4(),
    )
    with pytest.raises(ValidationError, match="continue the dashboard chain"):
        broken_chain.clean()

    with pytest.raises(ValidationError):
        DashboardTag(dashboard=dashboard, label="", key="", slot=1).clean()
    with pytest.raises(ValidationError, match="must be canonical"):
        DashboardTag(dashboard=dashboard, label="Risk", key="wrong", slot=1).clean()

    with pytest.raises(ValidationError, match="cannot exceed"):
        DashboardViewerState(
            user=incoming,
            dashboard=dashboard,
            last_viewed_at=timezone.now(),
            seen_publication_version=1,
        ).clean()

    archived = _dashboard(owner, state=Dashboard.State.ARCHIVED)
    with pytest.raises(ValidationError, match="does not accept revisions"):
        _revision(archived).clean()

    disabled_owner = _user("RELATED.DISABLED.OWNER", active=False)
    disabled_dashboard = _dashboard(disabled_owner)
    with pytest.raises(ValidationError, match="creator must be active"):
        _revision(disabled_dashboard, disabled_owner).clean()

    mismatched_revocation = ViewerGrant(
        dashboard=dashboard,
        viewer=incoming,
        created_by=owner,
        revoked_at=timezone.now(),
    )
    with pytest.raises(ValidationError, match="must either both be set"):
        mismatched_revocation.clean()


def test_access_request_creation_and_transition_edges() -> None:
    owner = _user("REQUEST.OWNER")
    requester = _user("REQUEST.REQUESTER")
    other = _user("REQUEST.OTHER")
    published = _published_dashboard(owner)

    with pytest.raises(ValidationError, match="must begin pending"):
        AccessRequest(
            dashboard=published,
            requester=requester,
            status=AccessRequest.Status.APPROVED,
        ).save()
    with pytest.raises(ValidationError, match="already has access"):
        AccessRequest(dashboard=published, requester=owner).save()
    with pytest.raises(ValidationError, match="not accepting"):
        AccessRequest(dashboard=_dashboard(owner), requester=requester).save()
    with pytest.raises(ValidationError, match="requester must be active"):
        AccessRequest(
            dashboard=published,
            requester=_user("REQUEST.DISABLED", active=False),
        ).save()

    with pytest.raises(ValidationError, match="resolution fields"):
        AccessRequest(
            dashboard=published,
            requester=requester,
            resolved_at=timezone.now(),
            resolved_by=owner,
        ).clean()

    requested_at = timezone.now()
    original = AccessRequest(
        id=1,
        dashboard=published,
        requester=requester,
        message="Retained",
        requested_at=requested_at,
    )

    changed_scope = copy(original)
    changed_scope.requester = other
    with pytest.raises(ImmutableRecordError, match="scope is immutable"):
        changed_scope._validate_transition(original)

    changed_time = copy(original)
    changed_time.requested_at = requested_at + timedelta(seconds=1)
    with pytest.raises(ImmutableRecordError, match="request time"):
        changed_time._validate_transition(original)
    copy(original)._validate_transition(original)

    invalid_status = copy(original)
    invalid_status.status = "invalid"
    with pytest.raises(ValidationError, match="resolution is not valid"):
        invalid_status._validate_transition(original)

    wrong_resolver = copy(original)
    wrong_resolver.status = AccessRequest.Status.DENIED
    wrong_resolver.resolved_at = timezone.now()
    wrong_resolver.resolved_by = requester
    with pytest.raises(ValidationError, match="resolver is not authorized"):
        wrong_resolver._validate_transition(original)

    unavailable_dashboard = _dashboard(owner, state=Dashboard.State.ARCHIVED)
    unavailable_original = AccessRequest(
        id=2,
        dashboard=unavailable_dashboard,
        requester=requester,
        requested_at=requested_at,
    )
    unavailable_approval = copy(unavailable_original)
    unavailable_approval.status = AccessRequest.Status.APPROVED
    unavailable_approval.resolved_at = timezone.now()
    unavailable_approval.resolved_by = owner
    with pytest.raises(ValidationError, match="cannot be approved"):
        unavailable_approval._validate_transition(unavailable_original)

    rewritten = copy(original)
    rewritten.status = AccessRequest.Status.DENIED
    rewritten.resolved_at = timezone.now()
    rewritten.resolved_by = owner
    rewritten.message = "Rewritten"
    with pytest.raises(ImmutableRecordError, match="rewrite request history"):
        rewritten._validate_transition(original)

    resolved = copy(original)
    resolved.status = AccessRequest.Status.DENIED
    resolved.resolved_at = timezone.now()
    resolved.resolved_by = owner

    stale_reopen = copy(resolved)
    stale_reopen.status = AccessRequest.Status.PENDING
    stale_reopen.resolved_at = None
    stale_reopen.resolved_by = None
    with pytest.raises(ValidationError, match="later request time"):
        stale_reopen._validate_transition(resolved)

    uncleared_reopen = copy(stale_reopen)
    uncleared_reopen.requested_at = requested_at + timedelta(seconds=1)
    uncleared_reopen.resolved_at = timezone.now()
    with pytest.raises(ValidationError, match="clear its prior resolution"):
        uncleared_reopen._validate_transition(resolved)

    inactive_requester = _user("REQUEST.REOPEN.DISABLED", active=False)
    inactive_original = copy(resolved)
    inactive_original.requester = inactive_requester
    inactive_reopen = copy(inactive_original)
    inactive_reopen.status = AccessRequest.Status.PENDING
    inactive_reopen.requested_at = requested_at + timedelta(seconds=1)
    inactive_reopen.resolved_at = None
    inactive_reopen.resolved_by = None
    with pytest.raises(ValidationError, match="requester must be active"):
        inactive_reopen._validate_transition(inactive_original)

    owner_original = copy(resolved)
    owner_original.requester = owner
    owner_reopen = copy(owner_original)
    owner_reopen.status = AccessRequest.Status.PENDING
    owner_reopen.requested_at = requested_at + timedelta(seconds=1)
    owner_reopen.resolved_at = None
    owner_reopen.resolved_by = None
    with pytest.raises(ValidationError, match="already has access"):
        owner_reopen._validate_transition(owner_original)

    draft_original = copy(resolved)
    draft_original.dashboard = _dashboard(owner)
    draft_reopen = copy(draft_original)
    draft_reopen.status = AccessRequest.Status.PENDING
    draft_reopen.requested_at = requested_at + timedelta(seconds=1)
    draft_reopen.resolved_at = None
    draft_reopen.resolved_by = None
    with pytest.raises(ValidationError, match="not accepting"):
        draft_reopen._validate_transition(draft_original)

    valid_reopen = copy(stale_reopen)
    valid_reopen.requested_at = requested_at + timedelta(seconds=1)
    valid_reopen._validate_transition(resolved)
    copy(resolved)._validate_transition(resolved)

    changed_resolved = copy(resolved)
    changed_resolved.message = "Changed after resolution"
    with pytest.raises(ImmutableRecordError, match="immutable until reopened"):
        changed_resolved._validate_transition(resolved)


def test_render_authorization_and_authorized_open_validation_edges() -> None:
    owner = _user("RENDER.OWNER")
    viewer = _user("RENDER.VIEWER")
    other = _user("RENDER.OTHER")
    dashboard = _published_dashboard(owner)
    other_dashboard = _published_dashboard(other)

    captured_preview = _authorization(
        dashboard,
        owner,
        audience=RenderAuthorization.Audience.PREVIEW,
    )
    captured_preview.authorized_open_captured_at = captured_preview.created_at
    with pytest.raises(ValidationError, match="capture marker"):
        captured_preview.clean()

    versioned_preview = _authorization(
        dashboard,
        owner,
        audience=RenderAuthorization.Audience.PREVIEW,
    )
    versioned_preview.publication_version = 1
    with pytest.raises(ValidationError, match="preview has no"):
        versioned_preview.clean()

    unversioned_viewer = _authorization(dashboard, owner)
    unversioned_viewer.publication_version = None
    with pytest.raises(ValidationError, match="must snapshot"):
        unversioned_viewer.clean()

    wrong_epoch = DashboardOwnershipTransfer(
        id=uuid4(),
        dashboard=other_dashboard,
        from_owner=other,
        to_owner=owner,
    )
    wrong_epoch_authorization = _authorization(dashboard, owner, epoch=wrong_epoch)
    with pytest.raises(ValidationError, match="epoch must belong"):
        wrong_epoch_authorization.clean()

    valid_epoch = DashboardOwnershipTransfer(
        id=uuid4(),
        dashboard=dashboard,
        from_owner=other,
        to_owner=owner,
    )
    valid_grant = ViewerGrant(
        id=uuid4(),
        dashboard=dashboard,
        viewer=viewer,
        created_by=owner,
    )
    epoch_and_grant = _authorization(dashboard, viewer, grant=valid_grant, epoch=valid_epoch)
    with pytest.raises(ValidationError, match="mutually exclusive"):
        epoch_and_grant.clean()

    preview_grant = _authorization(
        dashboard,
        viewer,
        audience=RenderAuthorization.Audience.PREVIEW,
        grant=valid_grant,
    )
    with pytest.raises(ValidationError, match="must target viewers"):
        preview_grant.clean()

    foreign_grant = ViewerGrant(
        id=uuid4(),
        dashboard=other_dashboard,
        viewer=viewer,
        created_by=other,
    )
    with pytest.raises(ValidationError, match="authorization dashboard"):
        _authorization(dashboard, viewer, grant=foreign_grant).clean()

    wrong_viewer_grant = ViewerGrant(
        id=uuid4(),
        dashboard=dashboard,
        viewer=other,
        created_by=owner,
    )
    with pytest.raises(ValidationError, match="authorization viewer"):
        _authorization(dashboard, viewer, grant=wrong_viewer_grant).clean()

    revoked_grant = ViewerGrant(
        id=uuid4(),
        dashboard=dashboard,
        viewer=viewer,
        created_by=owner,
        revoked_at=timezone.now(),
        revoked_by=owner,
    )
    with pytest.raises(ValidationError, match="must be active"):
        _authorization(dashboard, viewer, grant=revoked_grant).clean()

    with pytest.raises(ValidationError, match="must bind a grant"):
        _authorization(dashboard, viewer).clean()

    dashboard.last_ownership_transfer_id = valid_epoch.id
    with pytest.raises(ValidationError, match="current epoch"):
        _authorization(dashboard, owner).clean()

    stale_identity = _authorization(dashboard, owner, epoch=valid_epoch)
    stale_identity.viewer_auth_version = owner.auth_version + 1
    with pytest.raises(ValidationError, match="current user version"):
        stale_identity.clean()

    preview_source = _authorization(
        dashboard,
        owner,
        audience=RenderAuthorization.Audience.PREVIEW,
    )
    preview_open = AuthorizedOpen(
        source_authorization=preview_source,
        dashboard=dashboard,
        viewer=owner,
        revision=preview_source.revision,
        publication_version=1,
        opened_at=preview_source.created_at,
    )
    with pytest.raises(ValidationError, match="published viewer issues"):
        preview_open.clean()

    viewer_source = _authorization(dashboard, viewer, grant=valid_grant)
    mismatched_open = AuthorizedOpen(
        source_authorization=viewer_source,
        dashboard=dashboard,
        viewer=viewer,
        revision=viewer_source.revision,
        publication_version=2,
        opened_at=viewer_source.created_at,
    )
    with pytest.raises(ValidationError, match="snapshot must match"):
        mismatched_open.clean()


def test_database_backed_save_guards_can_reject_before_writing() -> None:
    owner = _user("SAVE.OWNER")
    viewer = _user("SAVE.VIEWER")
    dashboard = _published_dashboard(owner)
    now = timezone.now()

    original_state = DashboardViewerState(
        id=1,
        user=viewer,
        dashboard=dashboard,
        last_viewed_at=now,
        seen_publication_version=1,
    )
    changed_scope = copy(original_state)
    changed_scope.user = owner
    changed_scope._state.adding = False
    with patch.object(DashboardViewerState.objects, "get", return_value=original_state):
        with pytest.raises(ImmutableRecordError, match="scope is immutable"):
            changed_scope.save()

    older_view = copy(original_state)
    older_view.last_viewed_at = now - timedelta(seconds=1)
    older_view._state.adding = False
    with patch.object(DashboardViewerState.objects, "get", return_value=original_state):
        with pytest.raises(ImmutableRecordError, match="cannot move backward"):
            older_view.save()

    older_version = copy(original_state)
    older_version.seen_publication_version = 0
    older_version._state.adding = False
    with patch.object(DashboardViewerState.objects, "get", return_value=original_state):
        with pytest.raises(ImmutableRecordError, match="version cannot decrease"):
            older_version.save()

    original_authorization = _authorization(dashboard, owner)
    original_authorization.revoked_at = now
    changed_revocation = copy(original_authorization)
    changed_revocation.revoked_at = now + timedelta(seconds=1)
    changed_revocation._state.adding = False
    with patch.object(RenderAuthorization.objects, "get", return_value=original_authorization):
        with pytest.raises(ImmutableRecordError, match="revocation is immutable"):
            changed_revocation.save()

    original_authorization.authorized_open_captured_at = original_authorization.created_at
    changed_capture = copy(original_authorization)
    changed_capture.authorized_open_captured_at = None
    changed_capture._state.adding = False
    with patch.object(RenderAuthorization.objects, "get", return_value=original_authorization):
        with pytest.raises(ImmutableRecordError, match="capture marker is immutable"):
            changed_capture.save()

    source = _authorization(dashboard, owner)
    original_open = AuthorizedOpen(
        id=1,
        source_authorization=source,
        dashboard=dashboard,
        viewer=owner,
        revision=source.revision,
        publication_version=1,
        opened_at=source.created_at,
    )
    changed_open = copy(original_open)
    changed_open.publication_version = 2
    changed_open._state.adding = False
    with patch.object(AuthorizedOpen.objects, "get", return_value=original_open):
        with pytest.raises(ImmutableRecordError, match="source fields are immutable"):
            changed_open.save()

    original_open.aggregated_at = now
    changed_aggregation = copy(original_open)
    changed_aggregation.aggregated_at = now + timedelta(seconds=1)
    changed_aggregation._state.adding = False
    with patch.object(AuthorizedOpen.objects, "get", return_value=original_open):
        with pytest.raises(ImmutableRecordError, match="aggregation is one-way"):
            changed_aggregation.save()

    original_checkpoint = AnalyticsPipelineCheckpoint(
        pipeline_key="authorized_opens_v1",
        last_completed_open_id=5,
    )
    changed_key = copy(original_checkpoint)
    changed_key.pipeline_key = "changed"
    changed_key._state.adding = False
    with patch.object(
        AnalyticsPipelineCheckpoint.objects,
        "get",
        return_value=original_checkpoint,
    ):
        with pytest.raises(ImmutableRecordError, match="key is immutable"):
            changed_key.save()

    backward_checkpoint = copy(original_checkpoint)
    backward_checkpoint.last_completed_open_id = 4
    backward_checkpoint._state.adding = False
    with patch.object(
        AnalyticsPipelineCheckpoint.objects,
        "get",
        return_value=original_checkpoint,
    ):
        with pytest.raises(ImmutableRecordError, match="cannot move backward"):
            backward_checkpoint.save()

    original_reservation = StorageReservation(
        id=uuid4(),
        storage_key=StorageKey.generate().value,
        storage_state=StorageReservation.StorageState.OWNED,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        verified_size=None,
        verified_sha256="",
    )
    invalid_transition = copy(original_reservation)
    invalid_transition.storage_state = StorageReservation.StorageState.RESERVED
    invalid_transition._state.adding = False
    with patch.object(StorageReservation.objects, "get", return_value=original_reservation):
        with pytest.raises(ImmutableRecordError, match="transition is not allowed"):
            invalid_transition.save()
