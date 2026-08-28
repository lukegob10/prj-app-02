from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from agora.persistence.access import grant_project_viewer, revoke_project_viewer
from agora.persistence.models import AuditEvent, Dashboard, Revision, User, ViewerGrant
from agora.persistence.storage import FilesystemArtifactStorage
from agora.persistence.uploads import create_upload_revision
from agora.uploads import UploadPart

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner() -> User:
    return User.objects.create_user("ACCESS.OWNER")


@pytest.fixture
def viewer() -> User:
    return User.objects.create_user("ACCESS.VIEWER")


def authenticated_client(user: User, *, enforce_csrf_checks: bool = False) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.force_login(user)
    return client


def project_for(owner: User, *, name: str = "Access project") -> Dashboard:
    return Dashboard.objects.create(owner=owner, name=name)


def published_project(owner: User, root: Path, *, name: str) -> Dashboard:
    project = project_for(owner, name=name)
    revision = create_upload_revision(
        dashboard_id=project.id,
        created_by_id=owner.id,
        parts=[UploadPart("dashboard.html", [b"<html>shared</html>"], "text/html")],
        storage=FilesystemArtifactStorage(root),
    )
    project.refresh_from_db()
    project.published_revision = revision
    project.first_published_at = timezone.now()
    project.state = Dashboard.State.PUBLISHED
    project.save()
    return project


def test_owner_can_view_project_access_and_grant_a_canonical_soeid(
    owner: User,
    viewer: User,
) -> None:
    project = project_for(owner)
    client = authenticated_client(owner)

    page = client.get(reverse("project-access", args=[project.id]))
    assert page.status_code == 200
    assert owner.soeid.encode() in page.content
    assert b"Full control" in page.content
    assert b"Grant Viewer access" in page.content

    response = client.post(
        reverse("project-access", args=[project.id]),
        {"soeid": f"  {viewer.soeid.lower()}  "},
    )
    assert response.status_code == 302
    grant = ViewerGrant.objects.get(dashboard=project, viewer=viewer)
    assert grant.revoked_at is None
    assert grant.created_by_id == owner.id

    refreshed = client.get(reverse("project-access", args=[project.id]))
    assert refreshed.status_code == 200
    assert viewer.soeid.encode() in refreshed.content
    assert b"Active account" in refreshed.content
    assert b"Remove access" in refreshed.content


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("not valid!", "valid canonical SOEID"),
        ("ACCESS.UNKNOWN", "No active user was found"),
    ],
)
def test_grant_form_reports_safe_target_validation_messages(
    owner: User,
    target: str,
    expected: str,
) -> None:
    project = project_for(owner)
    response = authenticated_client(owner).post(
        reverse("project-access", args=[project.id]),
        {"soeid": target},
    )

    assert response.status_code == 200
    assert expected.encode() in response.content
    assert ViewerGrant.objects.filter(dashboard=project).exists() is False


def test_grant_form_reports_disabled_self_and_duplicate_targets(
    owner: User,
    viewer: User,
) -> None:
    project = project_for(owner)
    disabled = User.objects.create_user("ACCESS.DISABLED")
    disabled.is_active = False
    disabled.save(update_fields=("is_active",))
    client = authenticated_client(owner)

    disabled_response = client.post(
        reverse("project-access", args=[project.id]),
        {"soeid": disabled.soeid},
    )
    assert b"account is disabled" in disabled_response.content

    self_response = client.post(
        reverse("project-access", args=[project.id]),
        {"soeid": owner.soeid},
    )
    assert b"cannot grant yourself" in self_response.content

    grant_project_viewer(
        dashboard_id=project.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    duplicate_response = client.post(
        reverse("project-access", args=[project.id]),
        {"soeid": viewer.soeid},
    )
    assert b"already has Viewer access" in duplicate_response.content


def test_owner_can_revoke_a_viewer_with_confirmation_and_retry_idempotently(
    owner: User,
    viewer: User,
) -> None:
    project = project_for(owner)
    grant = grant_project_viewer(
        dashboard_id=project.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    client = authenticated_client(owner)
    revoke_url = reverse("project-grant-revoke", args=[project.id, grant.id])

    confirm = client.get(revoke_url)
    assert confirm.status_code == 200
    assert b"Remove Viewer access" in confirm.content
    assert b'name="confirm"' in confirm.content

    missing_confirmation = client.post(revoke_url, {})
    assert missing_confirmation.status_code == 200
    assert b"This field is required" in missing_confirmation.content
    assert b'aria-invalid="true"' in missing_confirmation.content
    assert b'aria-describedby="revoke-confirm-error"' in missing_confirmation.content

    first = client.post(revoke_url, {"confirm": "on"})
    assert first.status_code == 302
    grant.refresh_from_db()
    assert grant.revoked_at is not None
    audit_count = AuditEvent.objects.filter(event_type="grant.revoked", dashboard=project).count()

    retry = client.post(revoke_url, {"confirm": "on"})
    assert retry.status_code == 302
    assert (
        AuditEvent.objects.filter(event_type="grant.revoked", dashboard=project).count()
        == audit_count
    )


def test_non_owner_and_administrator_cannot_enumerate_project_access(
    owner: User,
    viewer: User,
) -> None:
    project = project_for(owner)
    administrator = User.objects.create_user("ACCESS.ADMIN")
    administrator.is_administrator = True
    administrator.save(update_fields=("is_administrator",))

    for user in (viewer, administrator):
        client = authenticated_client(user)
        assert client.get(reverse("project-access", args=[project.id])).status_code == 404
        assert (
            client.post(
                reverse("project-access", args=[project.id]),
                {"soeid": viewer.soeid},
            ).status_code
            == 404
        )


def test_access_routes_reject_unsupported_methods_and_require_csrf(
    owner: User,
    viewer: User,
) -> None:
    project = project_for(owner)
    grant = grant_project_viewer(
        dashboard_id=project.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    client = authenticated_client(owner)
    access_url = reverse("project-access", args=[project.id])
    revoke_url = reverse("project-grant-revoke", args=[project.id, grant.id])

    assert client.put(access_url, {"soeid": viewer.soeid}).status_code == 405
    assert client.delete(revoke_url).status_code == 405

    csrf_client = authenticated_client(owner, enforce_csrf_checks=True)
    assert csrf_client.post(revoke_url, {"confirm": "on"}).status_code == 403


def test_anonymous_access_routes_redirect_to_login(owner: User, viewer: User) -> None:
    project = project_for(owner)
    grant = ViewerGrant.objects.create(dashboard=project, viewer=viewer, created_by=owner)
    client = Client()

    for url in (
        reverse("project-access", args=[project.id]),
        reverse("project-grant-revoke", args=[project.id, grant.id]),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith(f"{reverse('login')}?next=")


def test_project_access_paginates_current_viewers_and_retains_disabled_status(
    owner: User,
) -> None:
    project = project_for(owner)
    viewers = [User.objects.create_user(f"ACCESS.VIEWER{i:02d}") for i in range(26)]
    for viewer in viewers:
        grant_project_viewer(
            dashboard_id=project.id,
            actor_id=owner.id,
            target_soeid=viewer.soeid,
        )
    disabled = viewers[-1]
    disabled.is_active = False
    disabled.save(update_fields=("is_active",))

    client = authenticated_client(owner)
    first = client.get(reverse("project-access", args=[project.id]))
    next_url = first.context["active_grants_page"].next_url
    assert next_url is not None
    second = client.get(next_url)
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first.context["active_grants"]) == 25
    assert len(second.context["active_grants"]) == 1
    assert b"ACCESS.VIEWER00" in first.content
    assert b"ACCESS.VIEWER25" not in first.content
    assert b"ACCESS.VIEWER25" in second.content
    assert b"Account disabled" in second.content


def test_project_pagination_preserves_mine_and_shared_scope(
    owner: User,
    viewer: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    project_for(owner, name="Owned first")
    project_for(owner, name="Owned second")
    mine = authenticated_client(owner).get(reverse("project-list"))
    assert mine.status_code == 200
    assert b'aria-label="Project results"' in mine.content
    assert b"cursor=" in mine.content
    assert b"page=" not in mine.content

    sharer = User.objects.create_user("ACCESS.SHARER")
    first_shared = published_project(sharer, tmp_path / "shared-one", name="Shared first")
    second_shared = published_project(sharer, tmp_path / "shared-two", name="Shared second")
    for project in (first_shared, second_shared):
        grant_project_viewer(
            dashboard_id=project.id,
            actor_id=sharer.id,
            target_soeid=viewer.soeid,
        )

    viewer_client = authenticated_client(viewer)
    with CaptureQueriesContext(connection) as shared_queries:
        shared = viewer_client.get(reverse("project-list"), {"scope": "shared"})
    assert shared.status_code == 200
    assert b"?scope=shared&amp;cursor=" in shared.content
    assert len(shared_queries) <= 3
    assert all("COUNT(" not in query["sql"].upper() for query in shared_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in shared_queries)


def test_project_list_is_result_bounded_and_query_constant(owner: User) -> None:
    client = authenticated_client(owner)
    project_for(owner, name="Baseline")
    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(reverse("project-list"))
    assert baseline.status_code == 200
    assert len(baseline_queries) <= 3

    for number in range(30):
        project_for(owner, name=f"Growth {number:02d}")
    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(reverse("project-list"))
    assert populated.status_code == 200
    assert len(populated.context["projects"]) == 25
    assert populated.context["project_page"].next_url is not None
    assert len(populated_queries) == len(baseline_queries)
    assert len(populated_queries) <= 3
    assert all("COUNT(" not in query["sql"].upper() for query in populated_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in populated_queries)


def test_revision_history_is_paginated_before_artifacts_are_prefetched(
    owner: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.REVISION_PAGE_SIZE", 1)
    project = project_for(owner)
    storage = FilesystemArtifactStorage(tmp_path / "revisions")
    for label in (b"first", b"second"):
        create_upload_revision(
            dashboard_id=project.id,
            created_by_id=owner.id,
            parts=[UploadPart("dashboard.html", [b"<html>" + label + b"</html>"], "text/html")],
            storage=storage,
        )

    client = authenticated_client(owner)
    first_page = client.get(reverse("project-detail", args=[project.id]))
    next_url = first_page.context["revision_page"].next_url
    assert next_url is not None
    second_page = client.get(next_url)
    assert first_page.status_code == 200
    assert b"Revision 2" in first_page.content
    assert b"Revision 1" not in first_page.content
    assert b'aria-label="Revision history"' in first_page.content
    assert second_page.status_code == 200
    assert b"Revision 1" in second_page.content
    assert b"Revision 2" not in second_page.content


def test_revision_history_default_page_is_bounded_to_25(owner: User) -> None:
    project = project_for(owner)
    for number in range(1, 27):
        Revision.objects.create(dashboard=project, number=number, created_by=owner)

    client = authenticated_client(owner)
    first = client.get(reverse("project-detail", args=[project.id]))
    next_url = first.context["revision_page"].next_url
    assert first.status_code == 200
    assert len(first.context["revisions"]) == 25
    assert next_url is not None

    second = client.get(next_url)
    assert second.status_code == 200
    assert len(second.context["revisions"]) == 1


def test_access_pagers_preserve_each_others_page(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.GRANT_PAGE_SIZE", 1)
    project = project_for(owner)
    active_users = [User.objects.create_user(f"ACCESS.ACTIVE{i}") for i in range(2)]
    history_users = [User.objects.create_user(f"ACCESS.HISTORY{i}") for i in range(2)]
    for target in active_users:
        grant_project_viewer(
            dashboard_id=project.id,
            actor_id=owner.id,
            target_soeid=target.soeid,
        )
    for target in history_users:
        grant = grant_project_viewer(
            dashboard_id=project.id,
            actor_id=owner.id,
            target_soeid=target.soeid,
        )
        revoke_project_viewer(
            dashboard_id=project.id,
            grant_id=grant.id,
            actor_id=owner.id,
        )

    client = authenticated_client(owner)
    first = client.get(reverse("project-access", args=[project.id]))
    active_cursor = first.context["active_grants_page"].next_cursor
    history_cursor = first.context["grant_history_page"].next_cursor
    assert active_cursor is not None
    assert history_cursor is not None
    response = client.get(
        reverse("project-access", args=[project.id]),
        {"active_cursor": active_cursor, "history_cursor": history_cursor},
    )
    assert response.status_code == 200
    active_previous = response.context["active_grants_page"].previous_url
    history_previous = response.context["grant_history_page"].previous_url
    assert active_previous is not None and "history_cursor=" in active_previous
    assert history_previous is not None and "active_cursor=" in history_previous


def test_project_detail_query_count_stays_bounded_as_revision_history_grows(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.REVISION_PAGE_SIZE", 1)
    project = project_for(owner)
    Revision.objects.create(dashboard=project, number=1, created_by=owner)
    Revision.objects.create(dashboard=project, number=2, created_by=owner)
    client = authenticated_client(owner)
    detail_url = reverse("project-detail", args=[project.id])
    first = client.get(detail_url)
    next_url = first.context["revision_page"].next_url
    assert next_url is not None

    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(next_url)
    assert baseline.status_code == 200

    for number in range(3, 43):
        Revision.objects.create(dashboard=project, number=number, created_by=owner)
    populated_first = client.get(detail_url)
    populated_next_url = populated_first.context["revision_page"].next_url
    assert populated_next_url is not None

    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(populated_next_url)
    assert populated.status_code == 200
    assert len(populated_queries) == len(baseline_queries)
    assert len(populated_queries) <= 5
    assert all("COUNT(" not in query["sql"].upper() for query in populated_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in populated_queries)


def test_project_access_query_count_stays_bounded_as_grant_history_grows(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.GRANT_PAGE_SIZE", 1)
    project = project_for(owner)
    for suffix in ("A", "B"):
        active = User.objects.create_user(f"ACCESS.QUERY.ACTIVE{suffix}")
        revoked = User.objects.create_user(f"ACCESS.QUERY.REVOKED{suffix}")
        ViewerGrant.objects.create(dashboard=project, viewer=active, created_by=owner)
        ViewerGrant.objects.create(
            dashboard=project,
            viewer=revoked,
            created_by=owner,
            revoked_at=timezone.now(),
            revoked_by=owner,
        )
    client = authenticated_client(owner)
    access_url = reverse("project-access", args=[project.id])
    first = client.get(access_url)
    baseline_active_cursor = first.context["active_grants_page"].next_cursor
    baseline_history_cursor = first.context["grant_history_page"].next_cursor
    assert baseline_active_cursor is not None
    assert baseline_history_cursor is not None

    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(
            access_url,
            {
                "active_cursor": baseline_active_cursor,
                "history_cursor": baseline_history_cursor,
            },
        )
    assert baseline.status_code == 200

    for number in range(30):
        current_viewer = User.objects.create_user(f"ACCESS.QUERY.CURRENT{number:02d}")
        former_viewer = User.objects.create_user(f"ACCESS.QUERY.FORMER{number:02d}")
        ViewerGrant.objects.create(
            dashboard=project,
            viewer=current_viewer,
            created_by=owner,
        )
        ViewerGrant.objects.create(
            dashboard=project,
            viewer=former_viewer,
            created_by=owner,
            revoked_at=timezone.now(),
            revoked_by=owner,
        )
    populated_first = client.get(access_url)
    populated_active_cursor = populated_first.context["active_grants_page"].next_cursor
    populated_history_cursor = populated_first.context["grant_history_page"].next_cursor
    assert populated_active_cursor is not None
    assert populated_history_cursor is not None

    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(
            access_url,
            {
                "active_cursor": populated_active_cursor,
                "history_cursor": populated_history_cursor,
            },
        )
    assert populated.status_code == 200
    assert len(populated_queries) == len(baseline_queries)
    assert len(populated_queries) <= 5
    assert all("COUNT(" not in query["sql"].upper() for query in populated_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in populated_queries)


def test_portal_read_paths_reject_tampered_or_cross_scope_cursors(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    project = project_for(owner, name="Cursor one")
    project_for(owner, name="Cursor two")
    client = authenticated_client(owner)

    first = client.get(reverse("project-list"))
    cursor = first.context["project_page"].next_cursor
    assert cursor is not None
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"

    assert client.get(reverse("project-list"), {"cursor": tampered}).status_code == 404
    assert (
        client.get(
            reverse("project-list"),
            {"scope": "shared", "cursor": cursor},
        ).status_code
        == 404
    )
    assert (
        client.get(reverse("project-detail", args=[project.id]), {"cursor": "invalid"}).status_code
        == 404
    )
    access_url = reverse("project-access", args=[project.id])
    assert client.get(access_url, {"active_cursor": "invalid"}).status_code == 404
    assert client.get(access_url, {"history_cursor": "invalid"}).status_code == 404


def test_keyset_navigation_skips_projects_deleted_after_cursor_issue(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    projects = [project_for(owner, name=f"Lifecycle {number}") for number in range(3)]
    moments = [timezone.now() - timedelta(minutes=number) for number in range(3)]
    for project, updated_at in zip(projects, moments, strict=True):
        Dashboard.objects.filter(id=project.id).update(updated_at=updated_at)

    client = authenticated_client(owner)
    first = client.get(reverse("project-list"))
    next_url = first.context["project_page"].next_url
    assert next_url is not None
    assert projects[0].name.encode() in first.content

    projects[1].state = Dashboard.State.DELETED
    projects[1].save(update_fields=("state", "updated_at"))
    second = client.get(next_url)
    assert second.status_code == 200
    assert projects[1].name.encode() not in second.content
    assert projects[2].name.encode() in second.content


def test_active_grant_cursor_survives_a_revocation_between_requests(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.GRANT_PAGE_SIZE", 1)
    project = project_for(owner)
    viewers = [User.objects.create_user(f"ACCESS.CHANGE.{suffix}") for suffix in ("A", "B", "C")]
    grants = [
        grant_project_viewer(
            dashboard_id=project.id,
            actor_id=owner.id,
            target_soeid=viewer.soeid,
        )
        for viewer in viewers
    ]

    client = authenticated_client(owner)
    first = client.get(reverse("project-access", args=[project.id]))
    next_url = first.context["active_grants_page"].next_url
    assert next_url is not None
    assert viewers[0].soeid.encode() in first.content

    revoke_project_viewer(
        dashboard_id=project.id,
        grant_id=grants[1].id,
        actor_id=owner.id,
    )
    second = client.get(next_url)
    assert second.status_code == 200
    assert viewers[1].soeid.encode() not in second.content
    assert viewers[2].soeid.encode() in second.content
