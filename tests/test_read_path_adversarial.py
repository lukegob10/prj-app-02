"""Adversarial lifecycle and scope checks around bounded portal reads."""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from agora.core.models import Dashboard, RenderAuthorization, Revision, User, ViewerGrant

pytestmark = pytest.mark.django_db(transaction=True)


class _Response(Protocol):
    status_code: int
    content: bytes


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _project(owner: User, name: str) -> Dashboard:
    return Dashboard.objects.create(owner=owner, name=name)


def _publish(project: Dashboard, owner: User, *, number: int = 1) -> Revision:
    revision = Revision.objects.create(dashboard=project, number=number, created_by=owner)
    project.latest_revision = revision
    project.published_revision = revision
    project.first_published_at = project.first_published_at or timezone.now()
    project.state = Dashboard.State.PUBLISHED
    project.save()
    return revision


def _assert_generic_not_found(response: _Response, *, hidden: bytes | None = None) -> None:
    assert response.status_code == 404
    content = response.content
    assert b"Project unavailable" in content
    assert b"does not exist or is not available to your SOEID" in content
    if hidden is not None:
        assert hidden not in content


def test_signed_cursors_cannot_cross_user_project_or_administrator_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    monkeypatch.setattr("agora.portal.views.REVISION_PAGE_SIZE", 1)
    monkeypatch.setattr("agora.portal.views.GRANT_PAGE_SIZE", 1)
    monkeypatch.setattr("agora.portal.views.USER_PAGE_SIZE", 1)

    owner = User.objects.create_user("ADVERSARY.OWNER")
    other_owner = User.objects.create_user("ADVERSARY.OTHER")
    admin = User.objects.create_user("ADVERSARY.ADMIN", is_administrator=True)
    other_admin = User.objects.create_user("ADVERSARY.ADMIN2", is_administrator=True)
    first_project = _project(owner, "Cursor source project")
    second_project = _project(owner, "Cursor destination project")
    _project(other_owner, "Other owner project")
    Revision.objects.create(dashboard=first_project, number=1, created_by=owner)
    Revision.objects.create(dashboard=first_project, number=2, created_by=owner)
    viewers = [User.objects.create_user(f"ADVERSARY.VIEWER{suffix}") for suffix in ("A", "B")]
    for viewer in viewers:
        ViewerGrant.objects.create(
            dashboard=first_project,
            viewer=viewer,
            created_by=owner,
        )

    owner_client = _client(owner)
    project_page = owner_client.get(reverse("project-list")).context["project_page"]
    assert project_page.next_cursor is not None
    cross_user = _client(other_owner).get(
        reverse("project-list"),
        {"cursor": project_page.next_cursor},
    )
    _assert_generic_not_found(cross_user)

    revision_page = owner_client.get(reverse("project-detail", args=[first_project.id])).context[
        "revision_page"
    ]
    assert revision_page.next_cursor is not None
    cross_project_revision = owner_client.get(
        reverse("project-detail", args=[second_project.id]),
        {"cursor": revision_page.next_cursor},
    )
    _assert_generic_not_found(
        cross_project_revision,
        hidden=first_project.name.encode(),
    )

    grant_page = owner_client.get(reverse("project-access", args=[first_project.id])).context[
        "active_grants_page"
    ]
    assert grant_page.next_cursor is not None
    cross_project_grant = owner_client.get(
        reverse("project-access", args=[second_project.id]),
        {"active_cursor": grant_page.next_cursor},
    )
    _assert_generic_not_found(cross_project_grant, hidden=viewers[0].soeid.encode())

    admin_page = _client(admin).get(reverse("admin-user-list")).context["user_page"]
    assert admin_page.next_cursor is not None
    cross_admin = _client(other_admin).get(
        reverse("admin-user-list"),
        {"cursor": admin_page.next_cursor},
    )
    _assert_generic_not_found(cross_admin)


def test_disabled_viewer_cannot_resume_a_previously_issued_shared_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    owner = User.objects.create_user("ADVERSARY.DISABLE.OWNER")
    viewer = User.objects.create_user("ADVERSARY.DISABLE.VIEWER")
    projects = [
        _project(owner, "Disabled cursor first"),
        _project(owner, "Disabled cursor second"),
    ]
    for project in projects:
        _publish(project, owner)
        ViewerGrant.objects.create(dashboard=project, viewer=viewer, created_by=owner)

    client = _client(viewer)
    first = client.get(reverse("project-list"), {"scope": "shared"})
    next_url = first.context["project_page"].next_url
    assert first.status_code == 200
    assert next_url is not None
    assert len(first.context["projects"]) == 1

    viewer.is_active = False
    viewer.save(update_fields=("is_active",))
    resumed = client.get(next_url)

    assert resumed.status_code == 200
    assert b"Dashboard workspace" not in resumed.content
    assert all(project.name.encode() not in resumed.content for project in projects)


def test_administrator_status_never_implies_dashboard_content_visibility() -> None:
    owner = User.objects.create_user("ADVERSARY.PRIVATE.OWNER")
    administrator = User.objects.create_user(
        "ADVERSARY.PRIVATE.ADMIN",
        is_administrator=True,
    )
    outsider = User.objects.create_user("ADVERSARY.PRIVATE.OUTSIDER")
    project = _project(owner, "Administrator must not see this dashboard")
    _publish(project, owner)
    admin_client = _client(administrator)

    assert admin_client.get(reverse("admin-user-list")).status_code == 200
    for response in (
        admin_client.get(reverse("home")),
        admin_client.get(reverse("project-list")),
        admin_client.get(reverse("project-list"), {"scope": "shared"}),
    ):
        assert response.status_code == 200
        assert project.name.encode() not in response.content

    protected_urls = (
        reverse("project-detail", args=[project.id]),
        reverse("project-access", args=[project.id]),
        reverse("project-view", args=[project.id]),
    )
    for url in protected_urls:
        _assert_generic_not_found(admin_client.get(url), hidden=project.name.encode())
        _assert_generic_not_found(_client(outsider).get(url), hidden=project.name.encode())

    missing = admin_client.get(reverse("project-detail", args=[uuid4()]))
    _assert_generic_not_found(missing, hidden=project.name.encode())
    assert RenderAuthorization.objects.count() == 0
