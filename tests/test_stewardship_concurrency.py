"""Bounded-query and concurrency gates for dashboard stewardship workflows."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from agora.core.enhancements import (
    EnhancementAccessDenied,
    request_dashboard_access,
    resolve_dashboard_access_request,
    transfer_dashboard_ownership,
)
from agora.core.models import (
    AccessRequest,
    Artifact,
    AuditEvent,
    Dashboard,
    DashboardOwnershipTransfer,
    Revision,
    User,
    ViewerGrant,
)
from agora.core.pagination import CursorPage
from agora.core.storage import StorageKey
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    issue_owner_preview,
    issue_published_view,
    resolve_render_authorization,
)

pytestmark = pytest.mark.django_db(transaction=True)

_ACCESS_REQUEST_TABLE = AccessRequest._meta.db_table.upper()
_VIEWER_GRANT_TABLE = ViewerGrant._meta.db_table.upper()


def _user(soeid: str) -> User:
    return User.objects.create_user(soeid)


def _client(user: User, *, enforce_csrf_checks: bool = False) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.force_login(user)
    return client


def _published_dashboard(owner: User, *, name: str = "Stewardship project") -> Dashboard:
    dashboard = Dashboard.objects.create(owner=owner, name=name, description="Retained description")
    revision = Revision.objects.create(dashboard=dashboard, number=1, created_by=owner)
    # A complete revision is normally produced by the upload service. This focused lane needs
    # valid metadata for the database invariant, but it never reads the corresponding bytes.
    Artifact.objects.create(
        revision=revision,
        kind=Artifact.Kind.HTML,
        logical_name="dashboard.html",
        storage_key=StorageKey.generate().value,
        byte_size=0,
        media_type="text/html",
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    Revision.objects.filter(id=revision.id).update(artifacts_locked=True)
    revision.refresh_from_db()
    published_at = timezone.now()
    dashboard.latest_revision = revision
    dashboard.published_revision = revision
    dashboard.first_published_at = published_at
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.save()
    dashboard.refresh_from_db()
    return dashboard


def _request(
    dashboard: Dashboard,
    requester: User,
    *,
    requested_at: datetime | None = None,
) -> AccessRequest:
    if requested_at is not None:
        return AccessRequest.objects.create(
            dashboard=dashboard,
            requester=requester,
            requested_at=requested_at,
        )
    access_request = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=requester.id,
    )
    return access_request


def _request_page(response: Any) -> CursorPage[AccessRequest]:
    page = response.context["request_page"]
    assert isinstance(page, CursorPage)
    return page


def _prepare_transfer(client: Client, dashboard: Dashboard, incoming_owner: User) -> str:
    selection = client.post(
        reverse("project-transfer", args=[dashboard.id]),
        {"incoming_owner_soeid": incoming_owner.soeid},
    )
    assert selection.status_code == 302
    confirmation_values = parse_qs(urlsplit(selection["Location"]).query).get("confirmation")
    assert confirmation_values is not None and len(confirmation_values) == 1
    token = confirmation_values[0]

    confirmation = client.get(selection["Location"])
    assert confirmation.status_code == 200
    assert confirmation.context["confirmation_token"] == token
    assert confirmation.context["incoming_owner_soeid"] == incoming_owner.soeid
    return token


def _confirm_transfer(client: Client, dashboard: Dashboard, token: str) -> Any:
    return client.post(
        reverse("project-transfer-confirm", args=[dashboard.id]),
        {"confirmation_token": token, "confirm": "on"},
    )


def _table_selects(queries: CaptureQueriesContext, table: str) -> list[str]:
    return [
        query["sql"]
        for query in queries.captured_queries
        if table in query["sql"].upper() and query["sql"].lstrip().upper().startswith("SELECT")
    ]


def _has_exact_row_limit(sql: str, rows: int) -> bool:
    """Recognize the same ORM slice on Oracle and the isolated SQLite verification backend."""

    normalized = sql.upper()
    return f"FETCH FIRST {rows} ROWS ONLY" in normalized or f"LIMIT {rows}" in normalized


def test_request_queue_is_pending_project_scoped_deterministic_and_fetches_one_sentinel() -> None:
    owner = _user("QUEUE.OWNER")
    other_owner = _user("QUEUE.OTHER.OWNER")
    dashboard = _published_dashboard(owner)
    other_dashboard = _published_dashboard(other_owner, name="Other queue")
    tied_at = timezone.now() - timedelta(days=1)

    baseline_requests = [
        _request(dashboard, _user(f"QUEUE.BASELINE.{number}"), requested_at=tied_at)
        for number in range(2)
    ]
    client = _client(owner)
    queue_url = reverse("project-access-requests", args=[dashboard.id])
    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(queue_url)
    assert baseline.status_code == 200
    assert {item.id for item in baseline.context["access_requests"]} == {
        item.id for item in baseline_requests
    }

    added = [
        _request(dashboard, _user(f"QUEUE.PENDING.{number:02d}"), requested_at=tied_at)
        for number in range(27)
    ]
    resolved_requester = _user("QUEUE.RESOLVED")
    resolved = _request(dashboard, resolved_requester, requested_at=tied_at)
    resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=resolved.id,
        actor_id=owner.id,
        resolution=AccessRequest.Status.DENIED,
    )
    foreign = _request(other_dashboard, _user("QUEUE.FOREIGN"), requested_at=tied_at)

    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(queue_url)
    assert populated.status_code == 200
    page = _request_page(populated)
    expected = sorted(
        [*baseline_requests, *added],
        key=lambda item: item.id,
        reverse=True,
    )
    assert [item.id for item in page] == [item.id for item in expected[:25]]
    assert len(page) == 25
    assert page.next_cursor is not None
    assert page.next_url is not None
    assert all(
        item.dashboard_id == dashboard.id and item.status == AccessRequest.Status.PENDING
        for item in page
    )
    assert resolved_requester.soeid.encode() not in populated.content
    assert foreign.requester.soeid.encode() not in populated.content

    baseline_selects = _table_selects(baseline_queries, _ACCESS_REQUEST_TABLE)
    populated_selects = _table_selects(populated_queries, _ACCESS_REQUEST_TABLE)
    assert len(baseline_queries) == len(populated_queries)
    assert len(baseline_selects) == len(populated_selects) == 1
    queue_sql = populated_selects[0].upper()
    assert _has_exact_row_limit(queue_sql, 26)
    assert "COUNT(" not in queue_sql
    assert " OFFSET " not in queue_sql

    with CaptureQueriesContext(connection) as next_queries:
        next_response = client.get(page.next_url)
    assert next_response.status_code == 200
    next_page = _request_page(next_response)
    assert [item.id for item in next_page] == [item.id for item in expected[25:]]
    assert len(next_queries) == len(populated_queries)
    assert all("COUNT(" not in query["sql"].upper() for query in next_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in next_queries)


def test_share_panel_queries_are_constant_bounded_and_never_count_or_load_requests() -> None:
    owner = _user("SHARE.QUERY.OWNER")
    dashboard = _published_dashboard(owner)
    active_users = [_user(f"SHARE.BASELINE.ACTIVE.{number}") for number in range(2)]
    history_users = [_user(f"SHARE.BASELINE.HISTORY.{number}") for number in range(2)]
    for viewer in active_users:
        ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    for viewer in history_users:
        ViewerGrant.objects.create(
            dashboard=dashboard,
            viewer=viewer,
            created_by=owner,
            revoked_at=timezone.now(),
            revoked_by=owner,
        )
    _request(dashboard, _user("SHARE.QUEUE.NOT.LOADED"))

    client = _client(owner)
    access_url = reverse("project-access", args=[dashboard.id])
    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(access_url)
    assert baseline.status_code == 200
    assert baseline.context["effective_access_page_count"] == 2

    for number in range(30):
        viewer = _user(f"SHARE.GROWTH.ACTIVE.{number:02d}")
        ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    disabled_users = [_user(f"SHARE.DISABLED.{number}") for number in range(2)]
    for viewer in disabled_users:
        viewer.is_active = False
        viewer.save(update_fields=("is_active", "updated_at"))
        ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)

    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(access_url)
    assert populated.status_code == 200
    assert len(populated.context["active_grants"]) == 25
    assert populated.context["active_grants_page"].next_cursor is not None
    assert populated.context["effective_access_page_count"] == sum(
        grant.viewer.is_active for grant in populated.context["active_grants"]
    )
    assert b"Account disabled" in populated.content
    assert b"SHARE.QUEUE.NOT.LOADED" not in populated.content

    assert len(populated_queries) == len(baseline_queries)
    assert all(
        _ACCESS_REQUEST_TABLE not in query["sql"].upper()
        for query in populated_queries.captured_queries
    )
    assert all("COUNT(" not in query["sql"].upper() for query in populated_queries)
    viewer_selects = [
        sql.upper()
        for sql in _table_selects(populated_queries, _VIEWER_GRANT_TABLE)
        if "COUNT(" not in sql.upper()
    ]
    assert len(viewer_selects) == 2
    assert all(_has_exact_row_limit(sql, 26) for sql in viewer_selects)
    assert all(" OFFSET " not in sql for sql in viewer_selects)


def test_request_cursor_rejects_tampering_cross_project_and_cross_owner_reuse() -> None:
    owner = _user("CURSOR.REQUEST.OWNER")
    incoming_owner = _user("CURSOR.REQUEST.INCOMING")
    dashboard = _published_dashboard(owner)
    second_dashboard = _published_dashboard(owner, name="Second cursor scope")
    tied_at = timezone.now() - timedelta(hours=1)
    requesters = [_user(f"CURSOR.REQUESTER.{number:02d}") for number in range(26)]
    for requester in requesters:
        _request(dashboard, requester, requested_at=tied_at)

    owner_client = _client(owner)
    first = owner_client.get(reverse("project-access-requests", args=[dashboard.id]))
    cursor = _request_page(first).next_cursor
    assert cursor is not None
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"

    tampered_response = owner_client.get(
        reverse("project-access-requests", args=[dashboard.id]),
        {"request_cursor": tampered},
    )
    cross_project = owner_client.get(
        reverse("project-access-requests", args=[second_dashboard.id]),
        {"request_cursor": cursor},
    )
    assert tampered_response.status_code == cross_project.status_code == 404
    assert all(requester.soeid.encode() not in cross_project.content for requester in requesters)

    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        incoming_owner_id=incoming_owner.id,
    )
    cross_owner = _client(incoming_owner).get(
        reverse("project-access-requests", args=[dashboard.id]),
        {"request_cursor": cursor},
    )
    assert cross_owner.status_code == 404
    assert all(requester.soeid.encode() not in cross_owner.content for requester in requesters)


def test_resolution_retries_and_opposite_decisions_preserve_the_first_transition() -> None:
    owner = _user("RESOLVE.OWNER")
    dashboard = _published_dashboard(owner)
    approved_requester = _user("RESOLVE.APPROVED")
    denied_requester = _user("RESOLVE.DENIED")
    approved = _request(dashboard, approved_requester)
    denied = _request(dashboard, denied_requester)
    client = _client(owner)

    approve_url = reverse(
        "project-access-request-approve",
        args=[dashboard.id, approved.id],
    )
    decline_approved_url = reverse(
        "project-access-request-decline",
        args=[dashboard.id, approved.id],
    )
    assert client.post(approve_url).status_code == 302
    transition_audits = AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type__in=("access.resolved", "grant.created"),
    ).count()
    assert client.post(approve_url).status_code == 302
    assert client.post(decline_approved_url).status_code == 302
    approved.refresh_from_db()
    assert approved.status == AccessRequest.Status.APPROVED
    grant = ViewerGrant.objects.get(
        dashboard=dashboard,
        viewer=approved_requester,
        revoked_at__isnull=True,
    )
    assert grant.created_by_id == owner.id
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type__in=("access.resolved", "grant.created"),
        ).count()
        == transition_audits
    )

    decline_url = reverse(
        "project-access-request-decline",
        args=[dashboard.id, denied.id],
    )
    approve_denied_url = reverse(
        "project-access-request-approve",
        args=[dashboard.id, denied.id],
    )
    assert client.post(decline_url).status_code == 302
    resolved_audits = AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type="access.resolved",
    ).count()
    assert client.post(decline_url).status_code == 302
    opposite = client.post(approve_denied_url, follow=True)
    assert opposite.status_code == 200
    assert b"changed or is no longer eligible" in opposite.content
    denied.refresh_from_db()
    assert denied.status == AccessRequest.Status.DENIED
    assert not ViewerGrant.objects.filter(dashboard=dashboard, viewer=denied_requester).exists()
    assert (
        AuditEvent.objects.filter(dashboard=dashboard, event_type="access.resolved").count()
        == resolved_audits
    )

    unknown = client.post(
        reverse("project-access-request-approve", args=[dashboard.id, 2**63 - 1]),
        follow=True,
    )
    assert unknown.status_code == 200
    assert b"changed or is no longer eligible" in unknown.content


def test_approval_rechecks_disabled_requester_and_preserves_a_duplicate_grant() -> None:
    owner = _user("RECHECK.OWNER")
    requester = _user("RECHECK.REQUESTER")
    dashboard = _published_dashboard(owner)
    access_request = _request(dashboard, requester)
    client = _client(owner)
    approve_url = reverse(
        "project-access-request-approve",
        args=[dashboard.id, access_request.id],
    )

    requester.is_active = False
    requester.save(update_fields=("is_active", "updated_at"))
    disabled = client.post(approve_url, follow=True)
    assert disabled.status_code == 200
    assert b"changed or is no longer eligible" in disabled.content
    assert b"Disabled account" in disabled.content
    access_request.refresh_from_db()
    assert access_request.status == AccessRequest.Status.PENDING
    assert not ViewerGrant.objects.filter(dashboard=dashboard, viewer=requester).exists()
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type="access.resolved",
        target_user=requester,
    ).exists()

    requester.is_active = True
    requester.save(update_fields=("is_active", "updated_at"))
    existing_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=requester,
        created_by=owner,
    )
    approved = client.post(approve_url)
    assert approved.status_code == 302
    access_request.refresh_from_db()
    assert access_request.status == AccessRequest.Status.APPROVED
    assert list(
        ViewerGrant.objects.filter(
            dashboard=dashboard,
            viewer=requester,
            revoked_at__isnull=True,
        ).values_list("id", flat=True)
    ) == [existing_grant.id]
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type="grant.created",
        target_user=requester,
    ).exists()
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type="access.resolved",
            target_user=requester,
        ).count()
        == 1
    )


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="real resolution-race coverage requires row-level locking",
)
def test_opposite_resolution_race_has_one_audited_winner() -> None:
    owner = _user("RACE.RESOLVE.OWNER")
    requester = _user("RACE.RESOLVE.REQUESTER")
    dashboard = _published_dashboard(owner)
    access_request = _request(dashboard, requester)

    def decide(resolution: AccessRequest.Status) -> str:
        close_old_connections()
        try:
            resolved = resolve_dashboard_access_request(
                dashboard_id=dashboard.id,
                request_id=access_request.id,
                actor_id=owner.id,
                resolution=resolution,
            )
        except EnhancementAccessDenied:
            return "changed"
        finally:
            close_old_connections()
        return resolved.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                decide,
                (AccessRequest.Status.APPROVED, AccessRequest.Status.DENIED),
            )
        )

    access_request.refresh_from_db()
    assert results.count("changed") == 1
    assert results.count(access_request.status) == 1
    assert access_request.status in {
        AccessRequest.Status.APPROVED,
        AccessRequest.Status.DENIED,
    }
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type="access.resolved",
            target_user=requester,
        ).count()
        == 1
    )
    active_grants = ViewerGrant.objects.filter(
        dashboard=dashboard,
        viewer=requester,
        revoked_at__isnull=True,
    )
    assert active_grants.count() == (
        1 if access_request.status == AccessRequest.Status.APPROVED else 0
    )


def test_transfer_preserves_dashboard_history_and_unrelated_grants_and_ends_old_authority() -> None:
    old_owner = _user("TRANSFER.ROUTE.OLD")
    incoming_owner = _user("TRANSFER.ROUTE.NEW")
    unrelated_viewer = _user("TRANSFER.ROUTE.UNRELATED")
    former_viewer = _user("TRANSFER.ROUTE.FORMER")
    dashboard = _published_dashboard(old_owner)
    revision = dashboard.latest_revision
    assert revision is not None
    incoming_request = _request(dashboard, incoming_owner)
    incoming_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=incoming_owner,
        created_by=old_owner,
    )
    unrelated_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=unrelated_viewer,
        created_by=old_owner,
    )
    historical_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=former_viewer,
        created_by=old_owner,
        revoked_at=timezone.now(),
        revoked_by=old_owner,
    )
    old_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=old_owner.id,
    )
    old_published = issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=old_owner.id,
    )
    incoming_published = issue_published_view(
        dashboard_id=dashboard.id,
        viewer_id=incoming_owner.id,
    )
    stable_url = reverse("project-view", args=[dashboard.id])
    stable_fields = (
        dashboard.id,
        dashboard.state,
        dashboard.description,
        dashboard.latest_revision_id,
        dashboard.published_revision_id,
        dashboard.first_published_at,
        dashboard.last_published_at,
        dashboard.publication_version,
    )

    old_client = _client(old_owner)
    token = _prepare_transfer(old_client, dashboard, incoming_owner)
    transferred = _confirm_transfer(old_client, dashboard, token)
    assert transferred.status_code == 302
    assert transferred["Location"] == reverse("project-list")

    dashboard.refresh_from_db()
    revision.refresh_from_db()
    incoming_grant.refresh_from_db()
    incoming_request.refresh_from_db()
    unrelated_grant.refresh_from_db()
    historical_grant.refresh_from_db()
    assert dashboard.owner_id == incoming_owner.id
    assert (
        dashboard.id,
        dashboard.state,
        dashboard.description,
        dashboard.latest_revision_id,
        dashboard.published_revision_id,
        dashboard.first_published_at,
        dashboard.last_published_at,
        dashboard.publication_version,
    ) == stable_fields
    assert reverse("project-view", args=[dashboard.id]) == stable_url
    assert revision.created_by_id == old_owner.id
    assert incoming_grant.revoked_at is not None
    assert incoming_grant.revoked_by_id == old_owner.id
    assert incoming_request.status == AccessRequest.Status.APPROVED
    assert incoming_request.resolved_by_id == old_owner.id
    assert unrelated_grant.revoked_at is None
    assert historical_grant.created_by_id == old_owner.id
    assert historical_grant.revoked_by_id == old_owner.id
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type="access.resolved",
            target_user=incoming_owner,
        ).count()
        == 1
    )

    for credential, audience in (
        (old_preview, "preview"),
        (old_published, "viewer"),
        (incoming_published, "viewer"),
    ):
        with pytest.raises(RenderAuthorizationDenied):
            resolve_render_authorization(credential.token, audience=audience)
    assert old_client.get(reverse("project-access", args=[dashboard.id])).status_code == 404
    assert old_client.get(reverse("project-transfer", args=[dashboard.id])).status_code == 404
    assert (
        _client(incoming_owner).get(reverse("project-access", args=[dashboard.id])).status_code
        == 200
    )
    with pytest.raises(RenderAuthorizationDenied):
        issue_owner_preview(
            dashboard_id=dashboard.id,
            revision_id=revision.id,
            viewer_id=old_owner.id,
        )
    issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=incoming_owner.id,
    )


def test_stale_transfer_confirmation_stays_invalid_after_transfer_back() -> None:
    first_owner = _user("TRANSFER.EPOCH.FIRST")
    second_owner = _user("TRANSFER.EPOCH.SECOND")
    final_owner = _user("TRANSFER.EPOCH.FINAL")
    dashboard = _published_dashboard(first_owner)
    revision = dashboard.latest_revision
    assert revision is not None
    first_epoch_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    first_client = _client(first_owner)
    stale_token = _prepare_transfer(first_client, dashboard, final_owner)

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
    transfer_count = DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count()
    stale = _confirm_transfer(first_client, dashboard, stale_token)
    assert stale.status_code == 302
    assert stale["Location"] == reverse("project-transfer", args=[dashboard.id])
    dashboard.refresh_from_db()
    assert dashboard.owner_id == first_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == transfer_count
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(first_epoch_preview.token, audience="preview")

    current_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    fresh_token = _prepare_transfer(first_client, dashboard, final_owner)
    assert _confirm_transfer(first_client, dashboard, fresh_token).status_code == 302
    dashboard.refresh_from_db()
    assert dashboard.owner_id == final_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 3
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(current_preview.token, audience="preview")


def test_confirmation_epoch_is_rechecked_atomically_after_an_aba_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_owner = _user("TRANSFER.ABA.FIRST")
    interim_owner = _user("TRANSFER.ABA.INTERIM")
    stale_target = _user("TRANSFER.ABA.TARGET")
    dashboard = _published_dashboard(first_owner)
    client = _client(first_owner)
    stale_token = _prepare_transfer(client, dashboard, stale_target)
    original_transfer = transfer_dashboard_ownership

    def inject_transfer_back_before_lock(**kwargs: Any) -> Dashboard:
        # The route has already checked the signed initial epoch at this point, but its requested
        # transfer has not yet acquired the dashboard row lock. Reproduce an ownership ABA in
        # that window. Calls which omit the optional expected epoch preserve the domain service's
        # existing callers; kwargs from the route must carry explicit None for the initial epoch.
        original_transfer(
            dashboard_id=dashboard.id,
            actor_id=first_owner.id,
            incoming_owner_id=interim_owner.id,
        )
        original_transfer(
            dashboard_id=dashboard.id,
            actor_id=interim_owner.id,
            incoming_owner_id=first_owner.id,
        )
        return original_transfer(**kwargs)

    monkeypatch.setattr(
        "agora.portal.stewardship.transfer_dashboard_ownership",
        inject_transfer_back_before_lock,
    )
    response = _confirm_transfer(client, dashboard, stale_token)

    dashboard.refresh_from_db()
    assert response.status_code == 302
    assert response["Location"] == reverse("project-transfer", args=[dashboard.id])
    assert dashboard.owner_id == first_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 2
    assert not DashboardOwnershipTransfer.objects.filter(
        dashboard=dashboard,
        to_owner=stale_target,
    ).exists()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="real transfer-race coverage requires row-level locking",
)
def test_concurrent_exact_epoch_transfer_confirmations_have_one_winner() -> None:
    owner = _user("RACE.TRANSFER.OWNER")
    first_target = _user("RACE.TRANSFER.FIRST")
    second_target = _user("RACE.TRANSFER.SECOND")
    dashboard = _published_dashboard(owner)
    first_client = _client(owner)
    second_client = _client(owner)
    first_token = _prepare_transfer(first_client, dashboard, first_target)
    second_token = _prepare_transfer(second_client, dashboard, second_target)

    def confirm(values: tuple[Client, str]) -> int:
        client, token = values
        close_old_connections()
        try:
            return int(_confirm_transfer(client, dashboard, token).status_code)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(
            executor.map(
                confirm,
                ((first_client, first_token), (second_client, second_token)),
            )
        )

    dashboard.refresh_from_db()
    assert 302 in statuses
    assert all(status in {302, 404} for status in statuses)
    assert dashboard.owner_id in {first_target.id, second_target.id}
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 1
    assert (
        AuditEvent.objects.filter(
            dashboard=dashboard,
            event_type="dashboard.ownership_transferred",
        ).count()
        == 1
    )


def test_stewardship_mutations_enforce_methods_and_csrf() -> None:
    owner = _user("METHOD.OWNER")
    requester = _user("METHOD.REQUESTER")
    incoming_owner = _user("METHOD.INCOMING")
    dashboard = _published_dashboard(owner)
    access_request = _request(dashboard, requester)
    client = _client(owner)
    queue_url = reverse("project-access-requests", args=[dashboard.id])
    approve_url = reverse(
        "project-access-request-approve",
        args=[dashboard.id, access_request.id],
    )
    decline_url = reverse(
        "project-access-request-decline",
        args=[dashboard.id, access_request.id],
    )
    transfer_url = reverse("project-transfer", args=[dashboard.id])
    confirm_url = reverse("project-transfer-confirm", args=[dashboard.id])

    assert client.post(queue_url).status_code == 405
    assert client.get(approve_url).status_code == 405
    assert client.get(decline_url).status_code == 405
    assert client.put(transfer_url).status_code == 405
    assert client.delete(confirm_url).status_code == 405

    token = _prepare_transfer(client, dashboard, incoming_owner)
    csrf_client = _client(owner, enforce_csrf_checks=True)
    assert csrf_client.post(approve_url).status_code == 403
    assert csrf_client.post(decline_url).status_code == 403
    assert (
        csrf_client.post(
            transfer_url,
            {"incoming_owner_soeid": incoming_owner.soeid},
        ).status_code
        == 403
    )
    assert (
        csrf_client.post(
            confirm_url,
            {"confirmation_token": token, "confirm": "on"},
        ).status_code
        == 403
    )
    access_request.refresh_from_db()
    dashboard.refresh_from_db()
    assert access_request.status == AccessRequest.Status.PENDING
    assert dashboard.owner_id == owner.id


def test_owner_only_queue_and_resolution_do_not_enumerate_requests() -> None:
    owner = _user("SCOPE.OWNER")
    outsider = _user("SCOPE.OUTSIDER")
    administrator = User.objects.create_user("SCOPE.ADMIN", is_administrator=True)
    requester = _user("SCOPE.REQUESTER")
    dashboard = _published_dashboard(owner, name="Hidden stewardship project")
    access_request = _request(dashboard, requester)
    urls = (
        reverse("project-access-requests", args=[dashboard.id]),
        reverse("project-access-request-approve", args=[dashboard.id, access_request.id]),
        reverse("project-access-request-decline", args=[dashboard.id, access_request.id]),
    )

    for principal in (outsider, administrator):
        client = _client(principal)
        queue = client.get(urls[0])
        approve = client.post(urls[1])
        decline = client.post(urls[2])
        for response in (queue, approve, decline):
            assert response.status_code == 404
            assert dashboard.name.encode() not in response.content
            assert requester.soeid.encode() not in response.content

    access_request.refresh_from_db()
    assert access_request.status == AccessRequest.Status.PENDING
    assert not ViewerGrant.objects.filter(dashboard=dashboard, viewer=requester).exists()

    missing_project = uuid4()
    missing = _client(owner).post(
        reverse("project-access-request-approve", args=[missing_project, access_request.id])
    )
    assert missing.status_code == 404
    assert dashboard.name.encode() not in missing.content
    assert requester.soeid.encode() not in missing.content
