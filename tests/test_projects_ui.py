from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from agora.persistence.models import AuditEvent, Dashboard, Revision, User, ViewerGrant
from agora.persistence.storage import FilesystemArtifactStorage
from agora.persistence.uploads import create_upload_revision
from agora.uploads import UploadPart

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner() -> User:
    return User.objects.create_user("PROJECT.OWNER")


@pytest.fixture
def viewer() -> User:
    return User.objects.create_user("PROJECT.VIEWER")


def authenticated_client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def published_project(
    owner: User,
    storage_root: Path,
    *,
    name: str = "Published project",
) -> Dashboard:
    project = Dashboard.objects.create(owner=owner, name=name, description="Shared reporting")
    revision = create_upload_revision(
        dashboard_id=project.id,
        created_by_id=owner.id,
        parts=[
            UploadPart(
                "dashboard.html",
                [b"<html><body>Shared</body></html>"],
                "text/html",
            )
        ],
        storage=FilesystemArtifactStorage(storage_root),
    )
    project.refresh_from_db()
    project.latest_revision = revision
    project.published_revision = revision
    project.first_published_at = timezone.now()
    project.state = Dashboard.State.PUBLISHED
    project.save()
    return project


def test_authenticated_home_and_navigation_expose_real_project_workflow(
    owner: User,
    tmp_path: Path,
) -> None:
    project = published_project(owner, tmp_path / "home", name="Risk overview")
    response = authenticated_client(owner).get(reverse("home"))

    assert response.status_code == 200
    assert b"Your projects, in one trusted place" in response.content
    assert b"Create new project" in response.content
    assert b"Risk overview" in response.content
    assert b'<body class="portal-page portal-page--home">' in response.content
    assert b'class="portal-home-hero"' in response.content
    assert b'class="portal-home-projects"' in response.content
    assert b'class="portal-home-project-stack"' in response.content
    assert b'class="portal-home-hero__stats"' not in response.content
    assert reverse("project-list").encode() in response.content
    assert (
        reverse("project-preview", args=[project.id, project.latest_revision_id]).encode()
        in response.content
    )


def test_project_creation_is_private_owner_scoped_and_audited(owner: User, viewer: User) -> None:
    owner_client = authenticated_client(owner)
    response = owner_client.post(
        reverse("project-create"),
        {"name": "  Quarterly liquidity  ", "description": "  Internal metrics  "},
    )

    project = Dashboard.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("project-detail", args=[project.id])
    assert project.owner_id == owner.id
    assert project.name == "Quarterly liquidity"
    assert project.description == "Internal metrics"
    assert project.state == Dashboard.State.DRAFT
    assert project.latest_revision_id is None
    assert project.published_revision_id is None
    event = AuditEvent.objects.get(event_type="dashboard.created")
    assert event.actor_id == owner.id
    assert event.dashboard_id == project.id

    assert owner_client.get(reverse("project-detail", args=[project.id])).status_code == 200
    denied = authenticated_client(viewer).get(reverse("project-detail", args=[project.id]))
    assert denied.status_code == 404
    assert project.name.encode() not in denied.content


def test_project_lists_separate_owned_from_actively_shared_publications(
    owner: User,
    viewer: User,
    tmp_path: Path,
) -> None:
    private = Dashboard.objects.create(owner=owner, name="Private draft")
    shared = published_project(owner, tmp_path / "shared")
    ViewerGrant.objects.create(dashboard=shared, viewer=viewer, created_by=owner)
    ViewerGrant.objects.create(
        dashboard=private,
        viewer=viewer,
        created_by=owner,
    )

    viewer_client = authenticated_client(viewer)
    shared_list = viewer_client.get(reverse("project-list"), {"scope": "shared"})
    assert shared_list.status_code == 200
    assert b'<body class="portal-page portal-page--workspace">' in shared_list.content
    assert shared.name.encode() in shared_list.content
    assert private.name.encode() not in shared_list.content
    assert owner.soeid.encode() in shared_list.content
    assert reverse("project-view", args=[shared.id]).encode() in shared_list.content
    assert viewer_client.get(reverse("project-detail", args=[shared.id])).status_code == 200

    owned_list = authenticated_client(owner).get(reverse("project-list"))
    assert b'<body class="portal-page portal-page--workspace">' in owned_list.content
    assert shared.name.encode() in owned_list.content
    assert private.name.encode() in owned_list.content
    assert (
        reverse("project-preview", args=[shared.id, shared.latest_revision_id]).encode()
        in owned_list.content
    )
    assert reverse("project-detail", args=[private.id]).encode() in owned_list.content

    viewer_home = viewer_client.get(reverse("home"))
    assert reverse("project-view", args=[shared.id]).encode() in viewer_home.content


def test_owner_upload_creates_an_immutable_revision_and_updates_detail(
    owner: User,
    tmp_path: Path,
) -> None:
    project = Dashboard.objects.create(owner=owner, name="Upload workflow")
    client = authenticated_client(owner)
    html = SimpleUploadedFile(
        "dashboard.html",
        b'<html><body><script>fetch("data.csv")</script></body></html>',
        content_type="text/html",
    )
    csv = SimpleUploadedFile(
        "data.csv",
        b"name,value\nitem,1\n",
        content_type="text/csv",
    )

    with override_settings(AGORA_ARTIFACT_ROOT=tmp_path / "private"):
        response = client.post(
            reverse("project-upload", args=[project.id]),
            {"html_file": html, "csv_files": [csv]},
        )

    assert response.status_code == 302
    revision = Revision.objects.get(dashboard=project)
    project.refresh_from_db()
    assert revision.number == 1
    assert project.latest_revision_id == revision.id
    assert project.state == Dashboard.State.DRAFT
    assert sorted(revision.artifacts.values_list("logical_name", flat=True)) == [
        "dashboard.html",
        "data.csv",
    ]
    assert AuditEvent.objects.filter(
        event_type="revision.created",
        actor=owner,
        dashboard=project,
        revision=revision,
    ).exists()

    detail = client.get(reverse("project-detail", args=[project.id]))
    assert b'<body class="portal-page portal-page--workspace">' in detail.content
    assert b"Revision 1" in detail.content
    assert b"dashboard.html" in detail.content
    assert b"data.csv" in detail.content


def test_upload_validation_error_creates_no_visible_revision(owner: User, tmp_path: Path) -> None:
    project = Dashboard.objects.create(owner=owner, name="Rejected upload")
    client = authenticated_client(owner)
    html = SimpleUploadedFile(
        "dashboard.html",
        b'<html><script src="https://example.com/app.js"></script></html>',
        content_type="text/html",
    )

    with override_settings(AGORA_ARTIFACT_ROOT=tmp_path / "private"):
        response = client.post(
            reverse("project-upload", args=[project.id]),
            {"html_file": html},
        )

    assert response.status_code == 200
    assert b"self-contained" in response.content
    assert Revision.objects.filter(dashboard=project).exists() is False
    assert [path for path in (tmp_path / "private").rglob("*") if path.is_file()] == []


def test_anonymous_project_routes_redirect_to_login(owner: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Private")
    client = Client()
    for url in (
        reverse("project-list"),
        reverse("project-create"),
        reverse("project-detail", args=[project.id]),
        reverse("project-upload", args=[project.id]),
        reverse(
            "project-preview",
            args=[project.id, "00000000-0000-0000-0000-000000000000"],
        ),
        reverse("project-view", args=[project.id]),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith(f"{reverse('login')}?next=")
