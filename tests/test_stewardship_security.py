"""Security-boundary coverage for project sharing and ownership stewardship."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from django.test import Client
from django.urls import resolve, reverse
from django.utils import timezone

from agora.persistence.enhancements import (
    publish_dashboard_revision,
    request_dashboard_access,
    resolve_dashboard_access_request,
    transfer_dashboard_ownership,
)
from agora.persistence.models import (
    AccessRequest,
    Artifact,
    AuditEvent,
    Dashboard,
    DashboardOwnershipTransfer,
    DashboardTag,
    RenderAuthorization,
    Revision,
    User,
    ViewerGrant,
)
from agora.persistence.services import ArtifactPayload, create_complete_revision
from agora.persistence.storage import FilesystemArtifactStorage
from agora.portal import stewardship as stewardship_views
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    issue_owner_preview,
    issue_published_view,
    resolve_render_authorization,
)

pytestmark = pytest.mark.django_db(transaction=True)

GENERIC_REQUEST_CONFIRMATION = (
    b"Request received. If this dashboard can accept requests, its owner will see it."
)


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _Response(Protocol):
    status_code: int
    content: bytes
    charset: str


def _visible_text(response: _Response) -> str:
    collector = _TextCollector()
    collector.feed(response.content.decode(response.charset))
    return " ".join(" ".join(collector.parts).split())


def _user(soeid: str, *, administrator: bool = False) -> User:
    user = User.objects.create_user(soeid)
    if administrator:
        user.is_administrator = True
        user.save(update_fields=("is_administrator",))
    return user


def _client(user: User, *, enforce_csrf_checks: bool = False) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.force_login(user)
    return client


def _complete_revision(root: Path, *, dashboard: Dashboard, owner: User) -> Revision:
    content = b"<!doctype html><title>Stewardship security fixture</title>"
    return create_complete_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        payloads=(
            ArtifactPayload(
                kind=Artifact.Kind.HTML,
                logical_name="dashboard.html",
                chunks=(content,),
                expected_size=len(content),
                expected_sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        storage=FilesystemArtifactStorage(root),
    )


def _published_dashboard(
    root: Path,
    *,
    owner: User,
    name: str = "Stewardship dashboard",
    description: str = "Stewardship description",
    publication_note: str = "Stewardship publication note",
    tag: str | None = None,
) -> tuple[Dashboard, Revision]:
    dashboard = Dashboard.objects.create(owner=owner, name=name, description=description)
    revision = _complete_revision(root, dashboard=dashboard, owner=owner)
    publish_dashboard_revision(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        revision_id=revision.id,
        publication_note=publication_note,
        data_as_of=timezone.now(),
    )
    dashboard.refresh_from_db()
    if tag is not None:
        DashboardTag.objects.create(
            dashboard=dashboard,
            label=tag,
            key=tag.casefold(),
            slot=1,
        )
    return dashboard, revision


def _confirmation(
    client: Client,
    *,
    dashboard: Dashboard,
    incoming_owner: User,
) -> tuple[str, str]:
    selection = client.post(
        reverse("project-transfer", args=[dashboard.id]),
        {"incoming_owner_soeid": incoming_owner.soeid},
    )
    assert selection.status_code == 302
    location = selection["Location"]
    parts = urlsplit(location)
    assert resolve(parts.path).url_name == "project-transfer-confirm"
    token = parse_qs(parts.query)["confirmation"]
    assert len(token) == 1
    return location, token[0]


def _confirm_transfer(client: Client, *, dashboard: Dashboard, token: str) -> Any:
    return client.post(
        reverse("project-transfer-confirm", args=[dashboard.id]),
        {"confirmation_token": token, "confirm": "on"},
    )


def _assert_warning(response: _Response, expected: bytes) -> None:
    assert response.status_code == 200
    assert expected in response.content


def test_stable_view_authorizes_current_access_and_hides_all_unavailable_metadata(
    tmp_path: Path,
) -> None:
    owner = _user("STEWARD.METADATA.OWNER")
    requester = _user("STEWARD.METADATA.REQUESTER")
    viewer = _user("STEWARD.METADATA.VIEWER")
    eligible, _ = _published_dashboard(
        tmp_path / "eligible",
        owner=owner,
        name="ELIGIBLE-NAME-7F20",
        description="ELIGIBLE-DESCRIPTION-7F20",
        publication_note="ELIGIBLE-PUBLICATION-NOTE-7F20",
        tag="EligibleTag7F20",
    )
    hidden = Dashboard.objects.create(
        owner=owner,
        name="HIDDEN-NAME-91C4",
        description="HIDDEN-DESCRIPTION-91C4",
    )
    DashboardTag.objects.create(
        dashboard=hidden,
        label="HiddenTag91C4",
        key="hiddentag91c4",
        slot=1,
    )
    ViewerGrant.objects.create(dashboard=eligible, viewer=viewer, created_by=owner)

    for authorized_user in (owner, viewer):
        response = _client(authorized_user).get(reverse("project-view", args=[eligible.id]))
        assert response.status_code == 200
        assert eligible.name.encode() in response.content
        assert b"/render/viewer/" in response.content

    requester_client = _client(requester)
    responses = [
        requester_client.get(reverse("project-view", args=[identifier]))
        for identifier in (eligible.id, hidden.id, uuid4())
    ]
    assert [response.status_code for response in responses] == [404, 404, 404]
    assert len({_visible_text(response) for response in responses}) == 1

    forbidden_values = (
        owner.soeid,
        eligible.name,
        eligible.description,
        eligible.publication_note,
        eligible.state,
        "EligibleTag7F20",
        hidden.name,
        hidden.description,
        hidden.state,
        "HiddenTag91C4",
    )
    for response in responses:
        assert response.context["request_submitted"] is False
        with pytest.raises(KeyError):
            response.context["project"]
        for value in forbidden_values:
            assert value.encode() not in response.content


def test_request_post_is_generic_deduplicated_reopenable_and_audited(tmp_path: Path) -> None:
    owner = _user("STEWARD.REQUEST.OWNER")
    requester = _user("STEWARD.REQUEST.REQUESTER")
    dashboard, _ = _published_dashboard(tmp_path / "request", owner=owner)
    hidden = Dashboard.objects.create(owner=owner, name="Hidden request target")
    client = _client(requester)
    request_url = reverse("project-view", args=[dashboard.id])

    first_response = client.post(request_url, {"message": "first owner-visible note"})
    duplicate_response = client.post(request_url, {"message": "must not replace pending note"})
    hidden_response = client.post(
        reverse("project-view", args=[hidden.id]),
        {"message": "hidden note"},
    )
    invalid_response = client.post(
        reverse("project-view", args=[uuid4()]),
        {"message": "invalid note"},
    )

    responses = (first_response, duplicate_response, hidden_response, invalid_response)
    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert len({_visible_text(response) for response in responses}) == 1
    for response in responses:
        assert GENERIC_REQUEST_CONFIRMATION in response.content
        assert response.context["request_submitted"] is True
        assert b"owner-visible note" not in response.content
        assert b"hidden note" not in response.content
        assert b"invalid note" not in response.content

    access_request = AccessRequest.objects.get(dashboard=dashboard, requester=requester)
    assert access_request.status == AccessRequest.Status.PENDING
    assert access_request.message == "first owner-visible note"
    assert AccessRequest.objects.count() == 1
    initial_requested_at = access_request.requested_at
    initial_audits = list(
        AuditEvent.objects.filter(
            dashboard=dashboard,
            actor=requester,
            event_type="access.requested",
        )
    )
    assert len(initial_audits) == 1
    assert initial_audits[0].metadata == {
        "request_id": access_request.id,
        "reopened": False,
    }

    decline = _client(owner).post(
        reverse("project-access-request-decline", args=[dashboard.id, access_request.id])
    )
    assert decline.status_code == 302
    access_request.refresh_from_db()
    assert access_request.status == AccessRequest.Status.DENIED
    assert access_request.resolved_by_id == owner.id

    reopened_response = client.post(request_url, {"message": "reopened owner-visible note"})
    assert reopened_response.status_code == 200
    assert GENERIC_REQUEST_CONFIRMATION in reopened_response.content
    assert b"reopened owner-visible note" not in reopened_response.content
    reopened_request = AccessRequest.objects.get(id=access_request.id)
    assert reopened_request.status == AccessRequest.Status.PENDING
    assert reopened_request.message == "reopened owner-visible note"
    assert reopened_request.requested_at > initial_requested_at
    assert reopened_request.resolved_at is None
    assert reopened_request.resolved_by_id is None
    assert AccessRequest.objects.filter(dashboard=dashboard, requester=requester).count() == 1
    reopened_audits = list(
        AuditEvent.objects.filter(
            dashboard=dashboard,
            actor=requester,
            event_type="access.requested",
        ).order_by("occurred_at", "id")
    )
    assert [event.metadata["reopened"] for event in reopened_audits] == [False, True]
    resolution_audit = AuditEvent.objects.get(
        dashboard=dashboard,
        actor=owner,
        target_user=requester,
        event_type="access.resolved",
    )
    assert resolution_audit.metadata == {
        "request_id": access_request.id,
        "resolution": AccessRequest.Status.DENIED,
    }


def test_request_queue_is_pending_only_escaped_and_owner_scoped(tmp_path: Path) -> None:
    owner = _user("STEWARD.QUEUE.OWNER")
    requester = _user("STEWARD.QUEUE.REQUESTER")
    resolved_requester = _user("STEWARD.QUEUE.RESOLVED")
    outsider = _user("STEWARD.QUEUE.OUTSIDER")
    administrator = _user("STEWARD.QUEUE.ADMIN", administrator=True)
    dashboard, _ = _published_dashboard(tmp_path / "queue", owner=owner)
    other_dashboard, _ = _published_dashboard(
        tmp_path / "queue-other",
        owner=owner,
        name="Other owner queue",
    )
    pending = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=requester.id,
        message="<script>alert('queue-secret')</script>",
    )
    resolved = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=resolved_requester.id,
        message="resolved request must not be listed",
    )
    resolve_dashboard_access_request(
        dashboard_id=dashboard.id,
        request_id=resolved.id,
        actor_id=owner.id,
        resolution=AccessRequest.Status.DENIED,
    )
    requester.is_active = False
    requester.save(update_fields=("is_active",))
    queue_url = reverse("project-access-requests", args=[dashboard.id])

    owner_response = _client(owner).get(queue_url)
    assert owner_response.status_code == 200
    assert [item.id for item in owner_response.context["access_requests"]] == [pending.id]
    assert requester.soeid.encode() in owner_response.content
    assert resolved_requester.soeid.encode() not in owner_response.content
    assert b"resolved request must not be listed" not in owner_response.content
    assert b"<script>alert('queue-secret')</script>" not in owner_response.content
    assert b"&lt;script&gt;alert" in owner_response.content
    assert b"Disabled" in owner_response.content

    cross_scope = _client(owner).post(
        reverse(
            "project-access-request-approve",
            args=[other_dashboard.id, pending.id],
        ),
        follow=True,
    )
    _assert_warning(cross_scope, b"changed or is no longer eligible")
    assert dashboard.name.encode() not in cross_scope.content
    pending.refresh_from_db()
    assert pending.status == AccessRequest.Status.PENDING
    assert not ViewerGrant.objects.filter(dashboard=dashboard, viewer=requester).exists()

    for unauthorized_user in (outsider, administrator):
        client = _client(unauthorized_user)
        assert client.get(reverse("project-access", args=[dashboard.id])).status_code == 404
        assert client.get(queue_url).status_code == 404
        assert client.get(reverse("project-transfer", args=[dashboard.id])).status_code == 404
        assert (
            client.post(
                reverse(
                    "project-access-request-decline",
                    args=[dashboard.id, pending.id],
                )
            ).status_code
            == 404
        )
    pending.refresh_from_db()
    assert pending.status == AccessRequest.Status.PENDING


def test_request_resolution_creates_or_preserves_one_grant_and_fails_stale_safely(
    tmp_path: Path,
) -> None:
    owner = _user("STEWARD.RESOLVE.OWNER")
    approved_requester = _user("STEWARD.RESOLVE.APPROVED")
    duplicate_requester = _user("STEWARD.RESOLVE.DUPLICATE")
    disabled_requester = _user("STEWARD.RESOLVE.DISABLED")
    dashboard, _ = _published_dashboard(tmp_path / "resolution", owner=owner)
    owner_client = _client(owner)

    approved = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=approved_requester.id,
    )
    approve_url = reverse(
        "project-access-request-approve",
        args=[dashboard.id, approved.id],
    )
    first = owner_client.post(approve_url, follow=True)
    assert first.status_code == 200
    assert b"Viewer access is approved" in first.content
    approved.refresh_from_db()
    grant = ViewerGrant.objects.get(
        dashboard=dashboard,
        viewer=approved_requester,
        revoked_at__isnull=True,
    )
    assert approved.status == AccessRequest.Status.APPROVED
    assert grant.created_by_id == owner.id
    transition_counts = {
        event_type: AuditEvent.objects.filter(
            dashboard=dashboard,
            target_user=approved_requester,
            event_type=event_type,
        ).count()
        for event_type in ("grant.created", "access.resolved")
    }
    assert transition_counts == {"grant.created": 1, "access.resolved": 1}

    retry = owner_client.post(approve_url, follow=True)
    assert retry.status_code == 200
    assert b"Viewer access is approved" in retry.content
    assert (
        ViewerGrant.objects.filter(
            dashboard=dashboard,
            viewer=approved_requester,
            revoked_at__isnull=True,
        ).count()
        == 1
    )
    assert {
        event_type: AuditEvent.objects.filter(
            dashboard=dashboard,
            target_user=approved_requester,
            event_type=event_type,
        ).count()
        for event_type in ("grant.created", "access.resolved")
    } == transition_counts

    duplicate = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=duplicate_requester.id,
    )
    existing_grant = ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=duplicate_requester,
        created_by=owner,
    )
    duplicate_response = owner_client.post(
        reverse(
            "project-access-request-approve",
            args=[dashboard.id, duplicate.id],
        ),
        follow=True,
    )
    assert duplicate_response.status_code == 200
    duplicate.refresh_from_db()
    assert duplicate.status == AccessRequest.Status.APPROVED
    assert list(
        ViewerGrant.objects.filter(
            dashboard=dashboard,
            viewer=duplicate_requester,
            revoked_at__isnull=True,
        ).values_list("id", flat=True)
    ) == [existing_grant.id]

    disabled = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=disabled_requester.id,
    )
    disabled_requester.is_active = False
    disabled_requester.save(update_fields=("is_active",))
    disabled_approval = owner_client.post(
        reverse(
            "project-access-request-approve",
            args=[dashboard.id, disabled.id],
        ),
        follow=True,
    )
    _assert_warning(disabled_approval, b"changed or is no longer eligible")
    disabled.refresh_from_db()
    assert disabled.status == AccessRequest.Status.PENDING
    assert not ViewerGrant.objects.filter(
        dashboard=dashboard,
        viewer=disabled_requester,
    ).exists()

    decline_url = reverse(
        "project-access-request-decline",
        args=[dashboard.id, disabled.id],
    )
    declined = owner_client.post(decline_url, follow=True)
    assert declined.status_code == 200
    assert b"was denied" in declined.content
    disabled.refresh_from_db()
    assert disabled.status == AccessRequest.Status.DENIED

    stale_opposite = owner_client.post(
        reverse(
            "project-access-request-approve",
            args=[dashboard.id, disabled.id],
        ),
        follow=True,
    )
    _assert_warning(stale_opposite, b"changed or is no longer eligible")
    unknown = owner_client.post(
        reverse(
            "project-access-request-decline",
            args=[dashboard.id, disabled.id + 10_000],
        ),
        follow=True,
    )
    _assert_warning(unknown, b"changed or is no longer eligible")
    disabled.refresh_from_db()
    assert disabled.status == AccessRequest.Status.DENIED
    assert not ViewerGrant.objects.filter(
        dashboard=dashboard,
        viewer=disabled_requester,
    ).exists()


def test_stewardship_mutations_restrict_methods_and_require_csrf(tmp_path: Path) -> None:
    owner = _user("STEWARD.CSRF.OWNER")
    requester = _user("STEWARD.CSRF.REQUESTER")
    incoming_owner = _user("STEWARD.CSRF.INCOMING")
    dashboard, _ = _published_dashboard(tmp_path / "csrf", owner=owner)
    access_request = request_dashboard_access(
        dashboard_id=dashboard.id,
        requester_id=requester.id,
    )
    owner_client = _client(owner)
    _, confirmation_token = _confirmation(
        owner_client,
        dashboard=dashboard,
        incoming_owner=incoming_owner,
    )
    view_url = reverse("project-view", args=[dashboard.id])
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

    assert _client(requester).put(view_url, {"message": "unsupported"}).status_code == 405
    assert owner_client.post(queue_url).status_code == 405
    assert owner_client.get(approve_url).status_code == 405
    assert owner_client.get(decline_url).status_code == 405
    assert owner_client.patch(transfer_url).status_code == 405
    assert owner_client.delete(confirm_url).status_code == 405

    request_csrf_client = _client(requester, enforce_csrf_checks=True)
    assert request_csrf_client.post(view_url, {"message": "blocked"}).status_code == 403
    owner_csrf_client = _client(owner, enforce_csrf_checks=True)
    assert owner_csrf_client.post(approve_url).status_code == 403
    assert owner_csrf_client.post(decline_url).status_code == 403
    assert (
        owner_csrf_client.post(
            transfer_url,
            {"incoming_owner_soeid": incoming_owner.soeid},
        ).status_code
        == 403
    )
    assert (
        owner_csrf_client.post(
            confirm_url,
            {"confirmation_token": confirmation_token, "confirm": "on"},
        ).status_code
        == 403
    )
    access_request.refresh_from_db()
    dashboard.refresh_from_db()
    assert access_request.status == AccessRequest.Status.PENDING
    assert dashboard.owner_id == owner.id
    assert not DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).exists()


def test_transfer_validation_and_signed_confirmation_fail_closed(tmp_path: Path) -> None:
    owner = _user("STEWARD.TRANSFER.VALIDATE.OWNER")
    incoming_owner = _user("STEWARD.TRANSFER.VALIDATE.INCOMING")
    outsider = _user("STEWARD.TRANSFER.VALIDATE.OUTSIDER")
    disabled = _user("STEWARD.TRANSFER.VALIDATE.DISABLED")
    disabled.is_active = False
    disabled.save(update_fields=("is_active",))
    dashboard, _ = _published_dashboard(tmp_path / "transfer-validation", owner=owner)
    other_dashboard, _ = _published_dashboard(
        tmp_path / "transfer-validation-other",
        owner=owner,
        name="Other transfer target",
    )
    client = _client(owner)
    transfer_url = reverse("project-transfer", args=[dashboard.id])

    for target, expected in (
        ("not valid!", b"valid canonical SOEID"),
        ("STEWARD.TRANSFER.UNKNOWN", b"No active user was found"),
        (disabled.soeid, b"No active user was found"),
        (owner.soeid, b"you already have Full control"),
    ):
        response = client.post(transfer_url, {"incoming_owner_soeid": target})
        assert response.status_code == 200
        assert expected in response.content
        dashboard.refresh_from_db()
        assert dashboard.owner_id == owner.id

    location, token = _confirmation(
        client,
        dashboard=dashboard,
        incoming_owner=incoming_owner,
    )
    confirmation_page = client.get(location)
    assert confirmation_page.status_code == 200
    assert confirmation_page.context["confirmation_token"] == token
    assert incoming_owner.soeid.encode() in confirmation_page.content
    assert dashboard.name.encode() in confirmation_page.content

    cross_project = _confirm_transfer(client, dashboard=other_dashboard, token=token)
    assert cross_project.status_code == 302
    assert cross_project["Location"] == reverse("project-transfer", args=[other_dashboard.id])
    actor_bound = _client(outsider).get(location)
    assert actor_bound.status_code == 404
    assert incoming_owner.soeid.encode() not in actor_bound.content
    assert dashboard.name.encode() not in actor_bound.content

    missing_acknowledgement = client.post(
        reverse("project-transfer-confirm", args=[dashboard.id]),
        {"confirmation_token": token},
    )
    assert missing_acknowledgement.status_code == 200
    assert b"This field is required" in missing_acknowledgement.content
    dashboard.refresh_from_db()
    assert dashboard.owner_id == owner.id

    tampered_token = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
    tampered = client.post(
        reverse("project-transfer-confirm", args=[dashboard.id]),
        {"confirmation_token": tampered_token, "confirm": "on"},
        follow=True,
    )
    _assert_warning(tampered, b"confirmation is invalid or no longer current")
    dashboard.refresh_from_db()
    assert dashboard.owner_id == owner.id
    assert not DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).exists()

    incoming_owner.is_active = False
    incoming_owner.save(update_fields=("is_active",))
    disabled_at_confirmation = _confirm_transfer(client, dashboard=dashboard, token=token)
    assert disabled_at_confirmation.status_code == 302
    assert disabled_at_confirmation["Location"] == reverse("project-transfer", args=[dashboard.id])
    dashboard.refresh_from_db()
    assert dashboard.owner_id == owner.id
    assert not DashboardOwnershipTransfer.objects.exists()


def test_transfer_preserves_stable_state_and_invalidates_old_authority(tmp_path: Path) -> None:
    old_owner = _user("STEWARD.TRANSFER.OLD")
    new_owner = _user("STEWARD.TRANSFER.NEW")
    other_viewer = _user("STEWARD.TRANSFER.VIEWER")
    dashboard, revision = _published_dashboard(
        tmp_path / "transfer",
        owner=old_owner,
        name="Transfer security dashboard",
        description="Transfer security description",
        publication_note="Transfer security publication",
        tag="TransferTag",
    )
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
    other_published = issue_published_view(dashboard_id=dashboard.id, viewer_id=other_viewer.id)
    stable_url = reverse("project-view", args=[dashboard.id])
    stable_state = (
        dashboard.id,
        dashboard.state,
        dashboard.latest_revision_id,
        dashboard.published_revision_id,
        dashboard.publication_version,
        dashboard.first_published_at,
        dashboard.last_published_at,
        dashboard.name,
        dashboard.description,
        dashboard.publication_note,
    )

    old_client = _client(old_owner)
    location, token = _confirmation(
        old_client,
        dashboard=dashboard,
        incoming_owner=new_owner,
    )
    confirmation_page = old_client.get(location)
    assert confirmation_page.status_code == 200
    assert new_owner.soeid.encode() in confirmation_page.content
    transferred = _confirm_transfer(old_client, dashboard=dashboard, token=token)
    assert transferred.status_code == 302
    assert transferred["Location"] == reverse("project-list")

    dashboard.refresh_from_db()
    revision.refresh_from_db()
    incoming_request.refresh_from_db()
    incoming_grant.refresh_from_db()
    other_grant.refresh_from_db()
    assert dashboard.owner_id == new_owner.id
    assert reverse("project-view", args=[dashboard.id]) == stable_url
    assert (
        dashboard.id,
        dashboard.state,
        dashboard.latest_revision_id,
        dashboard.published_revision_id,
        dashboard.publication_version,
        dashboard.first_published_at,
        dashboard.last_published_at,
        dashboard.name,
        dashboard.description,
        dashboard.publication_note,
    ) == stable_state
    assert revision.created_by_id == old_owner.id
    assert incoming_request.status == AccessRequest.Status.APPROVED
    assert incoming_request.resolved_by_id == old_owner.id
    assert incoming_grant.revoked_at is not None
    assert incoming_grant.revoked_by_id == old_owner.id
    assert other_grant.revoked_at is None
    assert DashboardTag.objects.filter(dashboard=dashboard, label="TransferTag").exists()
    transfer_audit = AuditEvent.objects.get(
        dashboard=dashboard,
        event_type="dashboard.ownership_transferred",
    )
    assert transfer_audit.actor_id == old_owner.id
    assert transfer_audit.target_user_id == new_owner.id
    request_audit = AuditEvent.objects.get(
        dashboard=dashboard,
        target_user=new_owner,
        event_type="access.resolved",
    )
    assert request_audit.actor_id == old_owner.id
    assert request_audit.metadata == {
        "request_id": incoming_request.id,
        "resolution": AccessRequest.Status.APPROVED,
    }

    for credential, audience in (
        (old_preview, RenderAuthorization.Audience.PREVIEW),
        (old_published, RenderAuthorization.Audience.VIEWER),
        (incoming_published, RenderAuthorization.Audience.VIEWER),
    ):
        with pytest.raises(RenderAuthorizationDenied):
            resolve_render_authorization(credential.token, audience=audience)
    assert (
        resolve_render_authorization(
            other_published.token,
            audience=RenderAuthorization.Audience.VIEWER,
        ).viewer.id
        == other_viewer.id
    )

    for route_name in ("project-access", "project-access-requests", "project-transfer"):
        assert old_client.get(reverse(route_name, args=[dashboard.id])).status_code == 404
    old_stable_view = old_client.get(stable_url)
    assert old_stable_view.status_code == 404
    assert dashboard.name.encode() not in old_stable_view.content

    new_client = _client(new_owner)
    assert new_client.get(reverse("project-access", args=[dashboard.id])).status_code == 200
    assert (
        new_client.get(reverse("project-access-requests", args=[dashboard.id])).status_code == 200
    )
    assert new_client.get(reverse("project-transfer", args=[dashboard.id])).status_code == 200
    assert new_client.get(stable_url).status_code == 200


def test_transfer_confirmation_epoch_cannot_replay_after_transfer_back(tmp_path: Path) -> None:
    first_owner = _user("STEWARD.EPOCH.FIRST")
    intended_owner = _user("STEWARD.EPOCH.INTENDED")
    temporary_owner = _user("STEWARD.EPOCH.TEMPORARY")
    dashboard, revision = _published_dashboard(tmp_path / "epoch", owner=first_owner)
    old_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    first_client = _client(first_owner)
    _, stale_token = _confirmation(
        first_client,
        dashboard=dashboard,
        incoming_owner=intended_owner,
    )

    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=first_owner.id,
        incoming_owner_id=temporary_owner.id,
    )
    transfer_dashboard_ownership(
        dashboard_id=dashboard.id,
        actor_id=temporary_owner.id,
        incoming_owner_id=first_owner.id,
    )
    dashboard.refresh_from_db()
    assert dashboard.owner_id == first_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 2

    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            old_preview.token,
            audience=RenderAuthorization.Audience.PREVIEW,
        )

    replay = _confirm_transfer(first_client, dashboard=dashboard, token=stale_token)
    assert replay.status_code == 302
    assert replay["Location"] == reverse("project-transfer", args=[dashboard.id])
    dashboard.refresh_from_db()
    assert dashboard.owner_id == first_owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 2
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type="dashboard.ownership_transferred",
        target_user=intended_owner,
    ).exists()

    current_preview = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=first_owner.id,
    )
    current_authorization = resolve_render_authorization(
        current_preview.token,
        audience=RenderAuthorization.Audience.PREVIEW,
    )
    assert current_authorization.authorization.owner_transfer_epoch_id == (
        dashboard.last_ownership_transfer_id
    )


def test_atomic_epoch_check_rejects_aba_after_view_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("STEWARD.TOCTOU.OWNER")
    intended_owner = _user("STEWARD.TOCTOU.INTENDED")
    temporary_owner = _user("STEWARD.TOCTOU.TEMPORARY")
    dashboard, _ = _published_dashboard(tmp_path / "toctou", owner=owner)
    owner_client = _client(owner)
    _, token = _confirmation(
        owner_client,
        dashboard=dashboard,
        incoming_owner=intended_owner,
    )
    original_transfer = transfer_dashboard_ownership

    def transfer_after_aba(
        *,
        dashboard_id: UUID,
        actor_id: UUID,
        incoming_owner_id: UUID,
        expected_transfer_id: UUID | None,
    ) -> Dashboard:
        original_transfer(
            dashboard_id=dashboard_id,
            actor_id=owner.id,
            incoming_owner_id=temporary_owner.id,
        )
        original_transfer(
            dashboard_id=dashboard_id,
            actor_id=temporary_owner.id,
            incoming_owner_id=owner.id,
        )
        return original_transfer(
            dashboard_id=dashboard_id,
            actor_id=actor_id,
            incoming_owner_id=incoming_owner_id,
            expected_transfer_id=expected_transfer_id,
        )

    monkeypatch.setattr(
        stewardship_views,
        "transfer_dashboard_ownership",
        transfer_after_aba,
    )
    response = _confirm_transfer(owner_client, dashboard=dashboard, token=token)

    assert response.status_code == 302
    assert response["Location"] == reverse("project-transfer", args=[dashboard.id])
    dashboard.refresh_from_db()
    assert dashboard.owner_id == owner.id
    assert DashboardOwnershipTransfer.objects.filter(dashboard=dashboard).count() == 2
    assert not AuditEvent.objects.filter(
        dashboard=dashboard,
        event_type="dashboard.ownership_transferred",
        target_user=intended_owner,
    ).exists()
