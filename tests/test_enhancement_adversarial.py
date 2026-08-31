"""Adversarial contract coverage for the enhancement foundation.

These tests deliberately exercise cross-feature seams: authorization rows are the
sole source of usage state, ownership changes must not rewrite retained actors, and
every navigation or analytics read must stay bounded without consulting raw opens.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Model, QuerySet, Subquery
from django.utils import timezone

from agora.core import analytics, enhancements
from agora.core.analytics import (
    aggregate_authorized_opens,
    capture_authorized_open,
    dashboard_daily_authorized_opens,
    dashboard_viewer_authorized_open_summaries,
    popular_authorized_dashboard_snapshots,
    purge_authorized_opens,
)
from agora.core.enhancement_queries import (
    dashboard_tags,
    dashboards_by_tag_key,
    pending_access_requests,
    recent_favorite_dashboards,
    recently_viewed_dashboards,
    stale_owned_dashboards,
)
from agora.core.enhancements import (
    EnhancementAccessDenied,
    EnhancementValidationError,
    confirm_dashboard_freshness,
    normalize_tag_key,
    publish_dashboard_revision,
    replace_dashboard_tags,
    request_dashboard_access,
    resolve_dashboard_access_request,
    rollback_dashboard_by_republish,
    set_dashboard_favorite,
    transfer_dashboard_ownership,
)
from agora.core.models import (
    AccessRequest,
    AnalyticsPipelineCheckpoint,
    Artifact,
    AuditEvent,
    AuthorizedOpen,
    Dashboard,
    DashboardFavorite,
    DashboardOpenDaily,
    DashboardOpenSnapshot,
    DashboardOwnershipTransfer,
    DashboardTag,
    DashboardViewerOpenSummary,
    DashboardViewerState,
    RenderAuthorization,
    Revision,
    User,
    ViewerGrant,
)
from agora.core.services import (
    ArtifactPayload,
    RevisionCreationError,
    create_complete_revision,
)
from agora.core.storage import FilesystemArtifactStorage
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    RenderAuthorizationUnavailable,
    issue_owner_preview,
    issue_published_view,
    resolve_render_authorization,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _user(soeid: str) -> User:
    return User.objects.create_user(soeid)


def _dashboard(owner: User, *, name: str = "Enhancement contract") -> Dashboard:
    return Dashboard.objects.create(owner=owner, name=name)


def _complete_revision(
    root: Path,
    *,
    dashboard: Dashboard,
    owner: User,
    marker: str,
) -> Revision:
    content = f"<!doctype html><title>{marker}</title>".encode()
    payload = ArtifactPayload(
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        chunks=(content,),
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    return create_complete_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        payloads=(payload,),
        storage=FilesystemArtifactStorage(root),
    )


def _publish(
    dashboard: Dashboard,
    owner: User,
    revision: Revision,
    *,
    now: datetime,
    note: str = "Initial release",
) -> Dashboard:
    publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=revision.id,
        publication_note=note,
        data_as_of=now - timedelta(hours=1),
        freshness_interval=timedelta(days=1),
        now=now,
    )
    dashboard.refresh_from_db()
    return dashboard


def _assert_sliced(queryset: QuerySet[Model], expected_limit: int) -> None:
    assert queryset.query.low_mark == 0
    assert queryset.query.high_mark == expected_limit


def _candidate_subqueries(queryset: QuerySet[Model]) -> list[Any]:
    candidates: list[Any] = []
    for child in queryset.query.where.children:
        rhs = vars(child).get("rhs")
        if isinstance(rhs, Subquery):
            candidates.append(rhs.query)
    return candidates


def _query_filter_fields(query: Any) -> set[str]:
    fields: set[str] = set()
    pending_nodes = [query.where]
    while pending_nodes:
        node = pending_nodes.pop()
        pending_nodes.extend(vars(node).get("children", ()))
        try:
            fields.add(cast(Any, node).lhs.target.name)
        except AttributeError:
            continue
    return fields


def _model_index_fields(model: type[Model]) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def _assert_bounded_candidate_window(
    queryset: QuerySet[Model],
    *,
    source_model: type[Model],
    expected_order: tuple[str, ...],
    expected_values: tuple[str, ...] = ("dashboard_id",),
    forbidden_candidate_fields: tuple[str, ...] = (),
) -> None:
    candidate_queries = _candidate_subqueries(queryset)
    assert len(candidate_queries) == 1
    candidate = candidate_queries[0]
    assert candidate.low_mark == 0
    assert candidate.high_mark == 100
    assert candidate.order_by == expected_order
    assert candidate.values_select == expected_values
    active_tables = {
        cast(Any, alias).table_name
        for alias_name, alias in candidate.alias_map.items()
        if candidate.alias_refcount.get(alias_name, 0) > 0
    }
    assert source_model._meta.db_table in active_tables
    if source_model is Dashboard:
        assert active_tables == {Dashboard._meta.db_table}
    else:
        assert Dashboard._meta.db_table not in active_tables

    candidate_fields = _query_filter_fields(candidate)
    for field in forbidden_candidate_fields:
        assert field not in candidate_fields


def test_tag_keys_are_canonical_duplicate_rejection_and_five_are_atomic() -> None:
    owner = _user("TAG.OWNER")
    dashboard = _dashboard(owner)

    assert normalize_tag_key("  Finance  ") == normalize_tag_key("finance")
    assert normalize_tag_key("  \uff26\uff49\uff4e\uff41\uff4e\uff43\uff45  ") == "finance"
    assert normalize_tag_key("Straße") == "strasse"
    tags = replace_dashboard_tags(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        labels=("  Finance  ", "Risk"),
    )

    assert len(tags) == 2
    assert {tag.key for tag in tags} == {
        normalize_tag_key("finance"),
        normalize_tag_key("risk"),
    }
    original = tuple(
        DashboardTag.objects.filter(dashboard=dashboard).order_by("key").values_list("id", "key")
    )

    with pytest.raises(EnhancementValidationError):
        replace_dashboard_tags(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            labels=("Finance", " finance "),
        )
    with pytest.raises(EnhancementValidationError):
        replace_dashboard_tags(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            labels=tuple(f"tag-{number}" for number in range(6)),
        )

    assert (
        tuple(
            DashboardTag.objects.filter(dashboard=dashboard)
            .order_by("key")
            .values_list("id", "key")
        )
        == original
    )


def test_personal_state_is_singleton_monotone_and_intersects_current_access(
    tmp_path: Path,
) -> None:
    owner = _user("PERSONAL.OWNER")
    viewer = _user("PERSONAL.VIEWER")
    outsider = _user("PERSONAL.OUTSIDER")
    draft = _dashboard(owner, name="Draft cannot be viewed")
    with pytest.raises(RenderAuthorizationDenied):
        issue_published_view(dashboard_id=draft.id, viewer_id=owner.id)
    assert not DashboardViewerState.objects.filter(user=owner, dashboard=draft).exists()
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "personal",
        dashboard=dashboard,
        owner=owner,
        marker="personal",
    )
    published_at = timezone.now()
    _publish(dashboard, owner, revision, now=published_at)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)

    first_favorite = set_dashboard_favorite(
        dashboard_id=dashboard.id,
        user_id=viewer.id,
        favorited=True,
    )
    second_favorite = set_dashboard_favorite(
        dashboard_id=dashboard.id,
        user_id=viewer.id,
        favorited=True,
    )
    assert first_favorite is not None
    assert second_favorite is not None
    assert first_favorite.id == second_favorite.id
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).count() == 1

    with pytest.raises(EnhancementAccessDenied):
        set_dashboard_favorite(
            dashboard_id=dashboard.id,
            user_id=outsider.id,
            favorited=True,
        )

    later = published_at + timedelta(minutes=2)
    issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=viewer.id,
        now=later,
    )
    state = DashboardViewerState.objects.get(user=viewer, dashboard=dashboard)
    first_viewed_at = state.last_viewed_at
    issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=viewer.id,
        now=later + timedelta(seconds=1),
    )
    retried = DashboardViewerState.objects.get(user=viewer, dashboard=dashboard)
    assert retried.id == state.id
    assert retried.last_viewed_at >= first_viewed_at
    assert retried.seen_publication_version == dashboard.publication_version
    assert DashboardViewerState.objects.filter(user=viewer, dashboard=dashboard).count() == 1
    recent = list(recently_viewed_dashboards(user_id=viewer.id, limit=1))
    assert len(recent) == 1
    assert cast(Any, recent[0]).has_new_publication is False

    publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=revision.id,
        publication_note="A new published update",
        data_as_of=None,
        freshness_interval=None,
        now=later + timedelta(seconds=1),
    )
    dashboard.refresh_from_db()
    recent = list(recently_viewed_dashboards(user_id=viewer.id, limit=1))
    assert len(recent) == 1
    assert cast(Any, recent[0]).has_new_publication is True

    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save(update_fields=("revoked_at", "revoked_by"))
    assert list(recent_favorite_dashboards(user_id=viewer.id, limit=10)) == []
    assert list(recently_viewed_dashboards(user_id=viewer.id, limit=10)) == []
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).exists()
    assert DashboardViewerState.objects.filter(user=viewer, dashboard=dashboard).exists()

    assert (
        set_dashboard_favorite(
            dashboard_id=dashboard.id,
            user_id=viewer.id,
            favorited=False,
        )
        is None
    )

    issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=owner.id,
        now=later + timedelta(minutes=1),
    )
    dashboard.published_revision = None
    dashboard.state = Dashboard.State.UNPUBLISHED
    dashboard.save(update_fields=("published_revision", "state", "updated_at"))
    assert list(recently_viewed_dashboards(user_id=owner.id, limit=10)) == []
    assert (
        set_dashboard_favorite(
            dashboard_id=dashboard.id,
            user_id=viewer.id,
            favorited=False,
        )
        is None
    )


def test_access_request_reopen_reuses_row_and_retains_audit_history(tmp_path: Path) -> None:
    owner = _user("REQUEST.OWNER")
    requester = _user("REQUEST.VIEWER")
    outsider = _user("REQUEST.OUTSIDER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "access-request",
        dashboard=dashboard,
        owner=owner,
        marker="request",
    )
    _publish(dashboard, owner, revision, now=timezone.now())

    private_dashboard = _dashboard(owner, name="Private request target")
    hidden_failures = []
    for hidden_dashboard_id, hidden_requester_id in (
        (private_dashboard.id, requester.id),
        (uuid4(), requester.id),
        (dashboard.id, owner.id),
    ):
        with pytest.raises(EnhancementAccessDenied) as captured:
            request_dashboard_access(
                dashboard_id=hidden_dashboard_id,
                requester_id=hidden_requester_id,
            )
        hidden_failures.append(str(captured.value))
    assert len(set(hidden_failures)) == 1

    first = request_dashboard_access(dashboard_id=dashboard.id, requester_id=requester.id)
    retried = request_dashboard_access(dashboard_id=dashboard.id, requester_id=requester.id)
    assert retried.id == first.id
    assert retried.status == AccessRequest.Status.PENDING
    assert AccessRequest.objects.filter(dashboard=dashboard, requester=requester).count() == 1
    assert [
        item.id
        for item in pending_access_requests(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            limit=1,
        )
    ] == [first.id]
    pending_audit_ids = set(
        AuditEvent.objects.filter(dashboard=dashboard, target_user=requester).values_list(
            "id", flat=True
        )
    )

    with pytest.raises(EnhancementAccessDenied):
        resolve_dashboard_access_request(
            dashboard_id=dashboard.id,
            request_id=first.id,
            actor_id=outsider.id,
            resolution=AccessRequest.Status.DENIED,
        )

    denied = resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=first.id,
        actor_id=owner.id,
        resolution=AccessRequest.Status.DENIED,
    )
    assert denied.status == AccessRequest.Status.DENIED
    assert denied.resolved_at is not None
    assert denied.resolved_by_id == owner.id
    assert (
        list(
            pending_access_requests(
                dashboard_id=dashboard.id,
                owner_id=owner.id,
                limit=1,
            )
        )
        == []
    )

    cancelling_requester = _user("REQUEST.CANCELLER")
    cancellable = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=cancelling_requester.id,
    )
    with pytest.raises(EnhancementAccessDenied):
        resolve_dashboard_access_request(
            dashboard_id=dashboard.id,
            request_id=cancellable.id,
            actor_id=owner.id,
            resolution=AccessRequest.Status.CANCELLED,
        )
    cancelled = resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=cancellable.id,
        actor_id=cancelling_requester.id,
        resolution=AccessRequest.Status.CANCELLED,
    )
    assert cancelled.status == AccessRequest.Status.CANCELLED
    assert cancelled.resolved_by_id == cancelling_requester.id
    reopened_cancelled = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=cancelling_requester.id,
    )
    assert reopened_cancelled.id == cancellable.id
    resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=cancellable.id,
        actor_id=cancelling_requester.id,
        resolution=AccessRequest.Status.CANCELLED,
    )

    reopened = request_dashboard_access(dashboard_id=dashboard.id, requester_id=requester.id)
    assert reopened.id == first.id
    assert reopened.status == AccessRequest.Status.PENDING
    assert reopened.resolved_at is None
    assert reopened.resolved_by_id is None
    assert AccessRequest.objects.filter(dashboard=dashboard, requester=requester).count() == 1
    assert [
        item.id
        for item in pending_access_requests(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            limit=1,
        )
    ] == [first.id]
    later_audit_ids = set(
        AuditEvent.objects.filter(dashboard=dashboard, target_user=requester).values_list(
            "id", flat=True
        )
    )
    assert pending_audit_ids < later_audit_ids

    approved = resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=first.id,
        actor_id=owner.id,
        resolution=AccessRequest.Status.APPROVED,
    )
    assert approved.status == AccessRequest.Status.APPROVED
    grant = ViewerGrant.objects.get(dashboard=dashboard, viewer=requester, revoked_at__isnull=True)
    assert grant.created_by_id == owner.id
    assert (
        list(
            pending_access_requests(
                dashboard_id=dashboard.id,
                owner_id=owner.id,
                limit=1,
            )
        )
        == []
    )


def test_access_approval_rechecks_eligibility_but_denial_can_close_retained_pending(
    tmp_path: Path,
) -> None:
    owner = _user("REQUEST.RECHECK.OWNER")
    requester = _user("REQUEST.RECHECK.VIEWER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "access-recheck",
        dashboard=dashboard,
        owner=owner,
        marker="request recheck",
    )
    _publish(dashboard, owner, revision, now=timezone.now())
    access_request = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=requester.id,
    )

    dashboard.published_revision = None
    dashboard.state = Dashboard.State.ARCHIVED
    dashboard.save(update_fields=("published_revision", "state", "updated_at"))
    with pytest.raises(EnhancementAccessDenied):
        resolve_dashboard_access_request(
            dashboard_id=dashboard.id,
            request_id=access_request.id,
            actor_id=owner.id,
            resolution=AccessRequest.Status.APPROVED,
        )
    access_request.refresh_from_db()
    assert access_request.status == AccessRequest.Status.PENDING
    assert not ViewerGrant.objects.filter(dashboard=dashboard, viewer=requester).exists()

    denied = resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=access_request.id,
        actor_id=owner.id,
        resolution=AccessRequest.Status.DENIED,
    )
    assert denied.status == AccessRequest.Status.DENIED


def test_publication_freshness_and_rollback_advance_without_rewriting_history(
    tmp_path: Path,
) -> None:
    owner = _user("PUBLICATION.OWNER")
    dashboard = _dashboard(owner)
    first = _complete_revision(
        tmp_path / "publication",
        dashboard=dashboard,
        owner=owner,
        marker="first",
    )
    second = _complete_revision(
        tmp_path / "publication",
        dashboard=dashboard,
        owner=owner,
        marker="second",
    )
    first_at = timezone.now()
    data_as_of = first_at - timedelta(hours=3)

    published = publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=first.id,
        publication_note="  First release  ",
        data_as_of=data_as_of,
        freshness_interval=timedelta(hours=6),
        now=first_at,
    )
    assert published.id == dashboard.id
    assert published.publication_version == 1
    assert published.published_revision_id == first.id
    assert published.first_published_at == first_at
    assert published.last_published_at == first_at
    assert published.publication_note == "First release"
    assert published.data_as_of == data_as_of
    assert published.freshness_interval_seconds == 6 * 60 * 60
    assert published.freshness_confirmed_at == first_at
    assert published.stale_after == first_at + timedelta(hours=6)

    republished_at = first_at + timedelta(hours=1)
    republished = publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=second.id,
        publication_note="Updated",
        data_as_of=data_as_of,
        freshness_interval=None,
        now=republished_at,
    )
    assert republished.publication_version == 2
    assert republished.published_revision_id == second.id
    assert republished.first_published_at == first_at
    assert republished.freshness_interval_seconds is None
    assert republished.freshness_confirmed_at is None
    assert republished.stale_after is None

    confirmed_at = republished_at + timedelta(hours=1)
    confirmed = confirm_dashboard_freshness(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        freshness_interval=timedelta(minutes=30),
        now=confirmed_at,
    )
    assert confirmed.publication_version == 2
    assert confirmed.data_as_of == data_as_of
    assert confirmed.freshness_confirmed_at == confirmed_at
    assert confirmed.stale_after == confirmed_at + timedelta(minutes=30)

    cleared_at = confirmed_at + timedelta(minutes=1)
    cleared = confirm_dashboard_freshness(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        data_as_of=None,
        freshness_interval=timedelta(minutes=30),
        now=cleared_at,
    )
    assert cleared.publication_version == 2
    assert cleared.data_as_of is None

    rollback_at = confirmed_at + timedelta(hours=1)
    rolled_back = rollback_dashboard_by_republish(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=first.id,
        publication_note="Rollback to known-good data",
        data_as_of=data_as_of,
        freshness_interval=timedelta(hours=1),
        now=rollback_at,
    )
    assert rolled_back.id == dashboard.id
    assert rolled_back.publication_version == 3
    assert rolled_back.published_revision_id == first.id
    assert rolled_back.latest_revision_id == second.id
    assert rolled_back.first_published_at == first_at
    assert Revision.objects.filter(dashboard=dashboard).count() == 2

    with pytest.raises(EnhancementValidationError):
        publish_dashboard_revision(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            revision_id=first.id,
            publication_note="x" * 241,
            data_as_of=None,
            freshness_interval=None,
            now=rollback_at + timedelta(minutes=1),
        )
    dashboard.refresh_from_db()
    assert dashboard.publication_version == 3

    with pytest.raises(EnhancementValidationError):
        confirm_dashboard_freshness(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            data_as_of=data_as_of,
            freshness_interval=timedelta(0),
            now=rollback_at,
        )
    with pytest.raises(EnhancementValidationError):
        confirm_dashboard_freshness(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            freshness_interval=timedelta(days=365, seconds=1),
            now=rollback_at,
        )
    with pytest.raises(EnhancementValidationError):
        confirm_dashboard_freshness(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            freshness_interval=timedelta(seconds=1, microseconds=1),
            now=rollback_at,
        )


def test_transfer_preserves_history_revokes_incoming_epoch_and_invalidates_credentials(
    tmp_path: Path,
) -> None:
    old_owner = _user("TRANSFER.OLD")
    new_owner = _user("TRANSFER.NEW")
    other_viewer = _user("TRANSFER.VIEWER")
    dashboard = _dashboard(old_owner)
    revision = _complete_revision(
        tmp_path / "transfer",
        dashboard=dashboard,
        owner=old_owner,
        marker="transfer",
    )
    _publish(dashboard, old_owner, revision, now=timezone.now())
    incoming_request = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=new_owner.id,
    )
    incoming_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=new_owner,
        created_by=old_owner,
    )
    other_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=other_viewer,
        created_by=old_owner,
    )
    old_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=old_owner.id,
    )
    old_published = issue_published_view(dashboard_id=dashboard.id, viewer_id=old_owner.id)
    incoming_published = issue_published_view(dashboard_id=dashboard.id, viewer_id=new_owner.id)
    stable = (
        dashboard.id,
        dashboard.latest_revision_id,
        dashboard.published_revision_id,
        dashboard.publication_version,
    )

    transferred = transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=old_owner.id,
        incoming_owner_id=new_owner.id,
        now=timezone.now(),
    )
    assert transferred.owner_id == new_owner.id
    assert (
        transferred.id,
        transferred.latest_revision_id,
        transferred.published_revision_id,
        transferred.publication_version,
    ) == stable

    incoming_grant.refresh_from_db()
    incoming_request.refresh_from_db()
    other_grant.refresh_from_db()
    revision.refresh_from_db()
    transfer_marker = DashboardOwnershipTransfer.objects.get(dashboard=dashboard)
    assert incoming_grant.revoked_at is not None
    assert incoming_grant.revoked_by_id == old_owner.id
    assert incoming_request.status == AccessRequest.Status.APPROVED
    assert incoming_request.resolved_at is not None
    assert incoming_request.resolved_by_id == old_owner.id
    assert other_grant.revoked_at is None
    assert revision.created_by_id == old_owner.id
    revision.full_clean()
    incoming_grant.full_clean()
    transfer_marker.full_clean()

    for token, audience in (
        (old_preview.token, RenderAuthorization.Audience.PREVIEW),
        (old_published.token, RenderAuthorization.Audience.VIEWER),
        (incoming_published.token, RenderAuthorization.Audience.VIEWER),
    ):
        with pytest.raises(RenderAuthorizationDenied):
            resolve_render_authorization(token, audience=audience)

    issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=new_owner.id,
    )
    with pytest.raises(RenderAuthorizationDenied):
        issue_owner_preview(
            dashboard_id=dashboard.id,
            revision_id=revision.id,
            viewer_id=old_owner.id,
        )

    event = AuditEvent.objects.get(event_type="dashboard.ownership_transferred")
    assert event.actor_id == old_owner.id
    assert event.target_user_id == new_owner.id
    assert event.dashboard_id == dashboard.id


def test_transfer_rejections_and_audit_failure_leave_everything_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("TRANSFER.ATOMIC.OWNER")
    incoming = _user("TRANSFER.ATOMIC.INCOMING")
    outsider = _user("TRANSFER.ATOMIC.OUTSIDER")
    dashboard = _dashboard(owner)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=incoming, created_by=owner)

    incoming.is_active = False
    incoming.save(update_fields=("is_active",))
    with pytest.raises(EnhancementAccessDenied):
        transfer_dashboard_ownership(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            incoming_owner_id=incoming.id,
        )
    with pytest.raises(EnhancementAccessDenied):
        transfer_dashboard_ownership(
            dashboard_id=dashboard.id,
            actor_id=outsider.id,
            incoming_owner_id=owner.id,
        )
    dashboard.refresh_from_db()
    grant.refresh_from_db()
    assert dashboard.owner_id == owner.id
    assert grant.revoked_at is None
    assert not DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).exists()
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type__in=("grant.revoked", "dashboard.ownership_transferred"),
    ).exists()

    incoming.is_active = True
    incoming.save(update_fields=("is_active",))
    archived = _dashboard(owner, name="Archived transfer target")
    archived.state = Dashboard.State.ARCHIVED
    archived.save(update_fields=("state", "updated_at"))
    with pytest.raises(EnhancementAccessDenied):
        transfer_dashboard_ownership(
            dashboard_id=archived.id,
            actor_id=owner.id,
            incoming_owner_id=incoming.id,
        )
    archived.refresh_from_db()
    assert archived.owner_id == owner.id

    original_create = AuditEvent.objects.create

    def fail_transfer_audit(**kwargs: object) -> AuditEvent:
        if kwargs.get("event_type") == "dashboard.ownership_transferred":
            raise RuntimeError("simulated audit outage")
        return original_create(**kwargs)

    monkeypatch.setattr(AuditEvent.objects, "create", fail_transfer_audit)
    with pytest.raises(RuntimeError, match="simulated audit outage"):
        transfer_dashboard_ownership(
            dashboard_id=dashboard.id,
            actor_id=owner.id,
            incoming_owner_id=incoming.id,
        )
    dashboard.refresh_from_db()
    grant.refresh_from_db()
    assert dashboard.owner_id == owner.id
    assert grant.revoked_at is None
    assert not DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).exists()
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type__in=("grant.revoked", "dashboard.ownership_transferred"),
    ).exists()


def test_transfer_back_is_a_new_chained_action_and_never_revives_old_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_owner = _user("TRANSFER.BACK.FIRST")
    second_owner = _user("TRANSFER.BACK.SECOND")
    dashboard = _dashboard(first_owner)
    frozen_at = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: frozen_at)
    revision = _complete_revision(
        tmp_path / "transfer-back",
        dashboard=dashboard,
        owner=first_owner,
        marker="transfer back",
    )
    _publish(dashboard, first_owner, revision, now=frozen_at)
    old_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    old_published = issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=first_owner.id,
    )
    grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=second_owner,
        created_by=first_owner,
    )

    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=first_owner.id,
        incoming_owner_id=second_owner.id,
    )
    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=second_owner.id,
        incoming_owner_id=first_owner.id,
    )

    dashboard.refresh_from_db()
    grant.refresh_from_db()
    latest_transfer_id = dashboard.last_ownership_transfer_id
    assert latest_transfer_id is not None
    latest_transfer = DashboardOwnershipTransfer.objects.select_related("previous_transfer").get(
        id=latest_transfer_id
    )
    first_transfer = latest_transfer.previous_transfer
    assert dashboard.owner_id == first_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 2
    assert first_transfer is not None
    assert first_transfer.from_owner_id == first_owner.id
    assert first_transfer.to_owner_id == second_owner.id
    assert first_transfer.previous_transfer_id is None
    assert latest_transfer.from_owner_id == second_owner.id
    assert latest_transfer.to_owner_id == first_owner.id
    assert latest_transfer.previous_transfer_id == first_transfer.id
    assert first_transfer.transferred_at == latest_transfer.transferred_at == frozen_at
    assert grant.revoked_at is not None
    assert grant.revoked_by_id == first_owner.id
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type="dashboard.ownership_transferred",
        ).count()
        == 2
    )
    for token, audience in (
        (old_preview.token, RenderAuthorization.Audience.PREVIEW),
        (old_published.token, RenderAuthorization.Audience.VIEWER),
    ):
        with pytest.raises(RenderAuthorizationDenied):
            resolve_render_authorization(token, audience=audience)

    current_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    resolved = resolve_render_authorization(
        current_preview.token,
        audience=RenderAuthorization.Audience.PREVIEW,
    )
    assert resolved.authorization.owner_transfer_epoch_id == latest_transfer.id


def test_successful_viewer_authorization_is_the_only_open_and_state_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("OPEN.OWNER")
    viewer = _user("OPEN.VIEWER")
    outsider = _user("OPEN.OUTSIDER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "authorized-open",
        dashboard=dashboard,
        owner=owner,
        marker="open",
    )
    opened_at = timezone.now()
    _publish(dashboard, owner, revision, now=opened_at - timedelta(minutes=1))
    ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)

    preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
        now=opened_at,
    )
    assert AuthorizedOpen.objects.count() == 0
    assert DashboardViewerState.objects.count() == 0
    resolve_render_authorization(preview.token, audience=RenderAuthorization.Audience.PREVIEW)
    preview_authorization = RenderAuthorization.objects.get(
        token_digest=hashlib.sha256(preview.token.encode("ascii")).hexdigest()
    )
    with pytest.raises(EnhancementAccessDenied):
        capture_authorized_open(
            authorization_id=preview_authorization.id,
            occurred_at=opened_at,
        )
    assert AuthorizedOpen.objects.count() == 0

    with pytest.raises(RenderAuthorizationDenied):
        issue_published_view(dashboard_id=dashboard.id, viewer_id=outsider.id, now=opened_at)
    assert AuthorizedOpen.objects.count() == 0

    def reject_capture(**_kwargs: object) -> bool:
        return False

    authorization_module = import_module("agora.rendering.authorization")
    with monkeypatch.context() as capture_failure:
        capture_failure.setattr(authorization_module, "capture_authorized_open", reject_capture)
        with pytest.raises(RenderAuthorizationUnavailable):
            issue_published_view(
                dashboard_id=dashboard.id,
                viewer_id=viewer.id,
                now=opened_at,
            )
    assert not RenderAuthorization.objects.filter(
        dashboard=dashboard,
        viewer=viewer,
        audience=RenderAuthorization.Audience.VIEWER,
    ).exists()
    assert AuthorizedOpen.objects.count() == 0
    assert DashboardViewerState.objects.count() == 0

    credential = issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=viewer.id,
        now=opened_at,
    )
    authorization = RenderAuthorization.objects.get(
        token_digest=hashlib.sha256(credential.token.encode("ascii")).hexdigest()
    )
    raw = AuthorizedOpen.objects.get(source_authorization=authorization)
    assert raw.dashboard_id == dashboard.id
    assert raw.viewer_id == viewer.id
    assert raw.opened_at == authorization.created_at
    assert not AuditEvent.objects.filter(event_type="dashboard.view_started").exists()
    state = DashboardViewerState.objects.get(dashboard=dashboard, user=viewer)
    assert state.last_viewed_at == authorization.created_at
    assert state.seen_publication_version == dashboard.publication_version

    assert (
        capture_authorized_open(
            authorization_id=authorization.id,
            occurred_at=opened_at + timedelta(seconds=1),
        )
        is False
    )
    resolve_render_authorization(
        credential.token,
        audience=RenderAuthorization.Audience.VIEWER,
    )
    assert AuthorizedOpen.objects.filter(source_authorization=authorization).count() == 1
    state.refresh_from_db()
    assert state.last_viewed_at == authorization.created_at

    publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=revision.id,
        publication_note="Republished",
        data_as_of=None,
        freshness_interval=None,
        now=opened_at + timedelta(minutes=1),
    )
    dashboard.refresh_from_db()
    raw.refresh_from_db()
    raw.full_clean()
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        )
    assert raw.publication_version == 1
    assert dashboard.publication_version == 2
    assert state.seen_publication_version == 1


def test_authorization_request_path_never_updates_rollups_or_popularity(
    tmp_path: Path,
) -> None:
    owner = _user("ROLLUP.OWNER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "rollup-path",
        dashboard=dashboard,
        owner=owner,
        marker="rollup",
    )
    _publish(dashboard, owner, revision, now=timezone.now())
    daily_count = DashboardOpenDaily.objects.count()
    viewer_summary_count = DashboardViewerOpenSummary.objects.count()
    snapshot_count = DashboardOpenSnapshot.objects.count()
    checkpoint_count = AnalyticsPipelineCheckpoint.objects.count()

    issue_published_view(dashboard_id=dashboard.id, viewer_id=owner.id)

    assert AuthorizedOpen.objects.count() == 1
    assert DashboardOpenDaily.objects.count() == daily_count
    assert DashboardViewerOpenSummary.objects.count() == viewer_summary_count
    assert DashboardOpenSnapshot.objects.count() == snapshot_count
    assert AnalyticsPipelineCheckpoint.objects.count() == checkpoint_count


def test_analytics_aggregation_and_retention_are_checkpointed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("PIPELINE.OWNER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "pipeline",
        dashboard=dashboard,
        owner=owner,
        marker="pipeline",
    )
    current_time = timezone.now()
    start = current_time - timedelta(days=91)
    monkeypatch.setattr(timezone, "now", lambda: start)
    _publish(dashboard, owner, revision, now=start)
    for _ in range(3):
        issue_published_view(
            dashboard_id=dashboard.id,
            viewer_id=owner.id,
            now=start,
        )

    monkeypatch.setattr(timezone, "now", lambda: current_time)
    raw_lock_shapes: list[bool] = []
    aggregate_candidate_shapes: list[tuple[int | None, tuple[str, ...]]] = []
    purge_candidate_shapes: list[tuple[int | None, tuple[str, ...]]] = []
    original_fetch_all = QuerySet._fetch_all

    def reject_sliced_oracle_lock(queryset: QuerySet[Model]) -> None:
        if queryset.model is AuthorizedOpen and queryset._result_cache is None:
            candidates = _candidate_subqueries(queryset)
            if candidates:
                assert len(candidates) == 1
                candidate = candidates[0]
                assert candidate.low_mark == 0
                assert candidate.values_select == ("id",)
                assert candidate.order_by == ("opened_at", "id")
                assert not queryset.query.is_sliced
                shape = (candidate.high_mark, candidate.order_by)
                candidate_fields = _query_filter_fields(candidate)
                if queryset.query.select_for_update:
                    raw_lock_shapes.append(queryset.query.is_sliced)
                    aggregate_candidate_shapes.append(shape)
                    assert candidate.high_mark == 2
                    assert {"aggregated_at", "opened_at"} <= candidate_fields
                else:
                    purge_candidate_shapes.append(shape)
                    assert candidate.high_mark == 1
                    assert "opened_at" in candidate_fields
                    assert "aggregated_at" not in candidate_fields
        original_fetch_all(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", reject_sliced_oracle_lock)
    through = current_time
    first_batch = aggregate_authorized_opens(
        through=through,
        batch_size=2,
    )
    assert first_batch.processed == 2
    assert DashboardOpenDaily.objects.get(dashboard=dashboard).authorized_open_count == 2
    checkpoint = AnalyticsPipelineCheckpoint.objects.get()
    first_checkpoint = checkpoint.last_completed_open_id

    second_batch = aggregate_authorized_opens(
        through=through,
        batch_size=2,
    )
    assert second_batch.processed == 1
    assert DashboardOpenDaily.objects.get(dashboard=dashboard).authorized_open_count == 3
    checkpoint.refresh_from_db()
    assert checkpoint.last_completed_open_id != first_checkpoint

    no_op = aggregate_authorized_opens(
        through=through,
        batch_size=2,
    )
    assert no_op.processed == 0
    assert raw_lock_shapes == [False, False, False]
    assert aggregate_candidate_shapes == [(2, ("opened_at", "id"))] * 3
    assert DashboardOpenDaily.objects.get(dashboard=dashboard).authorized_open_count == 3

    monkeypatch.setattr(timezone, "now", lambda: start)
    issue_published_view(dashboard_id=dashboard.id, viewer_id=owner.id, now=start)
    monkeypatch.setattr(timezone, "now", lambda: current_time)
    unaggregated = AuthorizedOpen.objects.order_by("-id").first()
    assert unaggregated is not None
    assert unaggregated.aggregated_at is None
    source_by_open = dict(AuthorizedOpen.objects.values_list("id", "source_authorization_id"))
    retention_cutoff = current_time - timedelta(days=90)
    purge_result = purge_authorized_opens(before=retention_cutoff, batch_size=1)
    assert purge_result.removed == 1
    assert purge_candidate_shapes == [(1, ("opened_at", "id"))]
    assert AuthorizedOpen.objects.count() == 3
    assert AuthorizedOpen.objects.filter(id=unaggregated.id, aggregated_at__isnull=True).exists()
    remaining_ids = set(AuthorizedOpen.objects.values_list("id", flat=True))
    deleted_ids = set(source_by_open) - remaining_ids
    assert len(deleted_ids) == 1
    deleted_open_id = deleted_ids.pop()
    assert (
        capture_authorized_open(
            authorization_id=source_by_open[deleted_open_id],
            occurred_at=through,
        )
        is False
    )
    assert AuthorizedOpen.objects.count() == 3
    with pytest.raises(ValueError):
        aggregate_authorized_opens(through=timezone.now(), batch_size=0)
    with pytest.raises(ValueError):
        aggregate_authorized_opens(through=timezone.now(), batch_size=1001)
    with pytest.raises(ValueError):
        purge_authorized_opens(before=timezone.now(), batch_size=0)
    with pytest.raises(ValueError):
        purge_authorized_opens(before=timezone.now(), batch_size=1001)
    with pytest.raises(ValueError):
        purge_authorized_opens(
            before=current_time - timedelta(days=89),
            batch_size=1,
        )


def test_all_navigation_queries_are_authorized_sliced_and_raw_open_free(
    tmp_path: Path,
) -> None:
    owner = _user("QUERY.ENHANCEMENT.OWNER")
    viewer = _user("QUERY.ENHANCEMENT.VIEWER")
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "enhancement-queries",
        dashboard=dashboard,
        owner=owner,
        marker="query",
    )
    now = timezone.now()
    _publish(dashboard, owner, revision, now=now)
    ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    replace_dashboard_tags(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        labels=("Finance", "Risk"),
    )
    set_dashboard_favorite(dashboard_id=dashboard.id, user_id=viewer.id, favorited=True)
    DashboardViewerState.objects.create(
        dashboard=dashboard,
        user=viewer,
        seen_publication_version=dashboard.publication_version,
        last_viewed_at=now,
    )
    requester = _user("QUERY.ENHANCEMENT.REQUESTER")
    request_dashboard_access(dashboard_id=dashboard.id, requester_id=requester.id)
    request_query = pending_access_requests(
        dashboard_id=dashboard.id,
        owner_id=owner.id,
        limit=1,
    )
    tag_lookup = dashboards_by_tag_key(
        principal_id=viewer.id,
        tag="  FINANCE  ",
        limit=1,
    )
    favorite_query = recent_favorite_dashboards(user_id=viewer.id, limit=1)
    recent_query = recently_viewed_dashboards(user_id=viewer.id, limit=1)
    stale_query = stale_owned_dashboards(
        owner_id=owner.id,
        as_of=now + timedelta(days=2),
        limit=1,
    )

    queries = (
        dashboard_tags(
            dashboard_id=dashboard.id,
            principal_id=owner.id,
            limit=1,
        ),
        tag_lookup,
        favorite_query,
        recent_query,
        request_query,
        stale_query,
    )
    raw_table = AuthorizedOpen._meta.db_table.upper()
    for queryset in queries:
        _assert_sliced(queryset, 1)
        assert raw_table not in str(queryset.query).upper()
        assert len(list(queryset)) <= 1
    assert [item.id for item in tag_lookup] == [dashboard.id]
    assert (
        list(
            dashboards_by_tag_key(
                principal_id=requester.id,
                tag="finance",
                limit=1,
            )
        )
        == []
    )
    _assert_bounded_candidate_window(
        tag_lookup,
        source_model=DashboardTag,
        expected_order=("dashboard_id",),
    )
    _assert_bounded_candidate_window(
        favorite_query,
        source_model=DashboardFavorite,
        expected_order=("-created_at", "-id"),
    )
    _assert_bounded_candidate_window(
        recent_query,
        source_model=DashboardViewerState,
        expected_order=("-last_viewed_at", "-id"),
    )
    _assert_bounded_candidate_window(
        stale_query,
        source_model=Dashboard,
        expected_order=("stale_after", "id"),
        expected_values=("id",),
        forbidden_candidate_fields=("state",),
    )
    assert ("key", "dashboard") in _model_index_fields(DashboardTag)
    assert ("user", "-created_at", "-id") in _model_index_fields(DashboardFavorite)
    assert ("user", "-last_viewed_at", "-id") in _model_index_fields(DashboardViewerState)
    assert ("dashboard", "status", "-requested_at", "-id") in _model_index_fields(AccessRequest)
    assert ("owner", "stale_after", "id") in _model_index_fields(Dashboard)

    private_dashboard = _dashboard(owner, name="Private tagged dashboard")
    replace_dashboard_tags(
        dashboard_id=private_dashboard.id,
        actor_id=owner.id,
        labels=("Private",),
    )
    assert (
        list(
            dashboards_by_tag_key(
                principal_id=owner.id,
                tag="private",
                limit=1,
            )
        )
        == []
    )
    private_dashboard.state = Dashboard.State.ARCHIVED
    private_dashboard.save(update_fields=("state", "updated_at"))
    assert (
        list(
            dashboards_by_tag_key(
                principal_id=owner.id,
                tag="private",
                limit=1,
            )
        )
        == []
    )

    dashboard.state = Dashboard.State.UNPUBLISHED
    dashboard.published_revision = None
    dashboard.save(update_fields=("state", "published_revision", "updated_at"))
    for principal_id in (owner.id, viewer.id):
        assert (
            list(
                dashboards_by_tag_key(
                    principal_id=principal_id,
                    tag="finance",
                    limit=1,
                )
            )
            == []
        )
    assert len(list(dashboard_tags(dashboard_id=dashboard.id, principal_id=owner.id))) == 2

    assert request_query.query.order_by == ("-requested_at", "-id")
    assert (
        list(
            pending_access_requests(
                dashboard_id=dashboard.id,
                owner_id=viewer.id,
                limit=1,
            )
        )
        == []
    )

    incoming_owner = _user("QUERY.ENHANCEMENT.NEW.OWNER")
    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        incoming_owner_id=incoming_owner.id,
    )
    assert (
        list(
            pending_access_requests(
                dashboard_id=dashboard.id,
                owner_id=owner.id,
                limit=1,
            )
        )
        == []
    )
    moved_queue = pending_access_requests(
        dashboard_id=dashboard.id,
        owner_id=incoming_owner.id,
        limit=1,
    )
    _assert_sliced(moved_queue, 1)
    assert [item.id for item in moved_queue] == [
        AccessRequest.objects.get(dashboard=dashboard, requester=requester).id
    ]

    for query_call in (
        lambda limit: dashboard_tags(
            dashboard_id=dashboard.id,
            principal_id=owner.id,
            limit=limit,
        ),
        lambda limit: dashboards_by_tag_key(
            principal_id=viewer.id,
            tag="finance",
            limit=limit,
        ),
        lambda limit: recent_favorite_dashboards(user_id=viewer.id, limit=limit),
        lambda limit: recently_viewed_dashboards(user_id=viewer.id, limit=limit),
        lambda limit: pending_access_requests(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            limit=limit,
        ),
        lambda limit: stale_owned_dashboards(owner_id=owner.id, as_of=now, limit=limit),
    ):
        with pytest.raises(ValueError):
            query_call(0)
        with pytest.raises(ValueError):
            query_call(10**9)


def test_aggregate_queries_are_bounded_project_scoped_and_follow_transfer(
    tmp_path: Path,
) -> None:
    owner = _user("ANALYTICS.QUERY.OWNER")
    new_owner = _user("ANALYTICS.QUERY.NEW")
    viewer = _user("ANALYTICS.QUERY.VIEWER")
    administrator = User.objects.create_user(
        "ANALYTICS.QUERY.ADMIN",
        is_administrator=True,
    )
    dashboard = _dashboard(owner)
    revision = _complete_revision(
        tmp_path / "analytics-query",
        dashboard=dashboard,
        owner=owner,
        marker="analytics query",
    )
    opened_at = timezone.now()
    _publish(dashboard, owner, revision, now=opened_at)
    ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    DashboardOpenDaily.objects.create(
        dashboard=dashboard,
        day=opened_at.date(),
        authorized_open_count=3,
    )
    DashboardViewerOpenSummary.objects.create(
        dashboard=dashboard,
        viewer=viewer,
        authorized_open_count=3,
        first_opened_at=opened_at,
        last_opened_at=opened_at,
    )
    DashboardOpenSnapshot.objects.create(
        dashboard=dashboard,
        authorized_open_count=3,
        last_opened_at=opened_at,
        captured_through_open_id=3,
    )

    viewer_popularity = popular_authorized_dashboard_snapshots(principal_id=viewer.id, limit=1)
    owner_queries = (
        dashboard_daily_authorized_opens(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            start=opened_at.date(),
            end=opened_at.date(),
            limit=1,
        ),
        dashboard_viewer_authorized_open_summaries(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            limit=1,
        ),
        viewer_popularity,
    )
    for queryset in owner_queries:
        _assert_sliced(queryset, 1)
        assert AuthorizedOpen._meta.db_table.upper() not in str(queryset.query).upper()
        assert len(list(queryset)) == 1

    _assert_bounded_candidate_window(
        viewer_popularity,
        source_model=ViewerGrant,
        expected_order=("-created_at", "-id"),
    )
    assert ("viewer", "revoked_at", "-created_at", "-id") in _model_index_fields(ViewerGrant)

    assert list(popular_authorized_dashboard_snapshots(principal_id=owner.id, limit=1)) == []
    with pytest.raises(ValueError):
        dashboard_daily_authorized_opens(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            start=opened_at.date(),
            end=opened_at.date(),
            limit=367,
        )
    with pytest.raises(ValueError):
        dashboard_viewer_authorized_open_summaries(
            dashboard_id=dashboard.id,
            owner_id=owner.id,
            limit=101,
        )
    with pytest.raises(ValueError):
        popular_authorized_dashboard_snapshots(principal_id=viewer.id, limit=101)

    assert (
        list(
            dashboard_daily_authorized_opens(
                dashboard_id=dashboard.id,
                owner_id=administrator.id,
                start=opened_at.date(),
                end=opened_at.date(),
                limit=1,
            )
        )
        == []
    )
    assert (
        list(popular_authorized_dashboard_snapshots(principal_id=administrator.id, limit=1)) == []
    )

    dashboard.published_revision = None
    dashboard.state = Dashboard.State.UNPUBLISHED
    dashboard.save(update_fields=("published_revision", "state", "updated_at"))
    assert list(popular_authorized_dashboard_snapshots(principal_id=viewer.id, limit=1)) == []

    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        incoming_owner_id=new_owner.id,
    )
    assert (
        list(
            dashboard_daily_authorized_opens(
                dashboard_id=dashboard.id,
                owner_id=owner.id,
                start=opened_at.date(),
                end=opened_at.date(),
                limit=1,
            )
        )
        == []
    )
    assert (
        len(
            list(
                dashboard_daily_authorized_opens(
                    dashboard_id=dashboard.id,
                    owner_id=new_owner.id,
                    start=opened_at.date(),
                    end=opened_at.date(),
                    limit=1,
                )
            )
        )
        == 1
    )


def test_raw_open_schema_and_import_boundary_cannot_grow_into_behavior_tracking() -> None:
    forbidden_fragments = {
        "click",
        "scroll",
        "filter",
        "iframe",
        "fetch",
        "ip",
        "agent",
        "referrer",
        "referer",
    }
    field_names = {field.name.lower() for field in AuthorizedOpen._meta.concrete_fields}
    assert not any(
        fragment in field_name for fragment in forbidden_fragments for field_name in field_names
    )
    assert "AuthorizedOpen" not in getattr(analytics, "__all__", ())
    assert "record_dashboard_view" not in getattr(enhancements, "__all__", ())
    assert not hasattr(enhancements, "record_dashboard_view")
    assert ("aggregated_at", "opened_at", "id") in _model_index_fields(AuthorizedOpen)
    assert ("opened_at", "id") in _model_index_fields(AuthorizedOpen)

    portal = import_module("agora.portal.views")
    assert portal.__file__ is not None
    portal_root = Path(portal.__file__).parent
    portal_source = "\n".join(
        source.read_text(encoding="utf-8") for source in portal_root.rglob("*.py")
    )
    assert "AuthorizedOpen" not in portal_source
    assert AuthorizedOpen._meta.db_table not in portal_source
    for raw_interface in (
        "capture_authorized_open",
        "aggregate_authorized_opens",
        "purge_authorized_opens",
    ):
        assert raw_interface not in portal_source


def test_old_owner_cannot_create_new_history_after_transfer(tmp_path: Path) -> None:
    old_owner = _user("HISTORY.OLD")
    new_owner = _user("HISTORY.NEW")
    viewer = _user("HISTORY.VIEWER")
    dashboard = _dashboard(old_owner)
    historical = _complete_revision(
        tmp_path / "history-old",
        dashboard=dashboard,
        owner=old_owner,
        marker="historical",
    )
    historical_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=viewer,
        created_by=old_owner,
    )
    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=old_owner.id,
        incoming_owner_id=new_owner.id,
    )

    dashboard.refresh_from_db()
    historical.refresh_from_db()
    historical_grant.refresh_from_db()
    historical.full_clean()
    historical_grant.full_clean()
    assert historical.created_by_id == old_owner.id
    assert historical_grant.created_by_id == old_owner.id

    with pytest.raises(RevisionCreationError):
        _complete_revision(
            tmp_path / "history-rejected",
            dashboard=dashboard,
            owner=old_owner,
            marker="rejected",
        )
    new_revision = _complete_revision(
        tmp_path / "history-new",
        dashboard=dashboard,
        owner=new_owner,
        marker="new owner",
    )
    assert new_revision.created_by_id == new_owner.id

    invalid_grant = ViewerGrant(
        dashboard=dashboard,
        viewer=_user("HISTORY.OTHER"),
        created_by=old_owner,
    )
    with pytest.raises(ValidationError):
        invalid_grant.full_clean()
