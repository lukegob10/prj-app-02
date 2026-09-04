from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from agora.core.models import AuditEvent, Dashboard, Revision, User, ViewerGrant
from agora.core.storage import FilesystemArtifactStorage
from agora.core.uploads import create_upload_revision
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


def test_authenticated_root_is_the_single_projects_workspace(
    owner: User,
    tmp_path: Path,
) -> None:
    project = published_project(owner, tmp_path / "home", name="Risk overview")
    response = authenticated_client(owner).get(reverse("home"))

    assert response.status_code == 200
    assert b"Dashboard workspace" in response.content
    assert b"<h1>Projects</h1>" in response.content
    assert b"Create new project" in response.content
    assert b"My projects" in response.content
    assert b"Shared with me" in response.content
    assert b"Risk overview" in response.content
    assert b'<body class="portal-page portal-page--workspace">' in response.content
    assert b"Quick access" not in response.content
    assert b"Welcome back" not in response.content
    assert response.content.count(b">Projects</a>") == 2
    assert b">Home</a>" not in response.content
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


def test_owner_can_rename_dashboard_to_a_reused_name(owner: User, viewer: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Original dashboard")
    same_owner_project = Dashboard.objects.create(owner=owner, name="Monthly overview")
    other_owner_project = Dashboard.objects.create(owner=viewer, name="Monthly overview")
    client = authenticated_client(owner)
    rename_url = reverse("project-rename", args=[project.id])

    form = client.get(rename_url)
    assert form.status_code == 200
    assert b"Rename dashboard" in form.content
    assert b'value="Original dashboard"' in form.content
    assert b"Names do not need to be unique" in form.content

    response = client.post(rename_url, {"name": "  Monthly overview  "})

    assert response.status_code == 302
    assert response["Location"] == reverse("project-detail", args=[project.id])
    project.refresh_from_db()
    assert project.name == "Monthly overview"
    assert same_owner_project.name == "Monthly overview"
    assert other_owner_project.name == "Monthly overview"
    assert len({project.id, same_owner_project.id, other_owner_project.id}) == 3
    assert AuditEvent.objects.filter(
        event_type="dashboard.renamed",
        actor=owner,
        dashboard=project,
        metadata={},
    ).exists()

    detail = client.get(response["Location"])
    assert detail.status_code == 200
    assert b"Monthly overview" in detail.content
    assert rename_url.encode() in detail.content


def test_dashboard_rename_is_validated_and_owner_scoped(owner: User, viewer: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Restricted name")
    rename_url = reverse("project-rename", args=[project.id])

    invalid = authenticated_client(owner).post(rename_url, {"name": "   "})
    assert invalid.status_code == 200
    assert b"This field is required" in invalid.content

    outsider = authenticated_client(viewer)
    assert outsider.get(rename_url).status_code == 404
    denied = outsider.post(rename_url, {"name": "Unauthorized change"})
    assert denied.status_code == 404
    assert project.name.encode() not in denied.content

    project.refresh_from_db()
    assert project.name == "Restricted name"
    assert (
        AuditEvent.objects.filter(event_type="dashboard.renamed", dashboard=project).exists()
        is False
    )


def test_archived_dashboard_cannot_be_renamed(owner: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Archived dashboard")
    project.state = Dashboard.State.ARCHIVED
    project.save(update_fields=("state", "updated_at"))
    client = authenticated_client(owner)

    detail = client.get(reverse("project-detail", args=[project.id]))
    assert detail.status_code == 200
    assert reverse("project-rename", args=[project.id]).encode() not in detail.content
    assert client.get(reverse("project-rename", args=[project.id])).status_code == 404


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
    assert reverse("project-view", args=[shared.id]).encode() not in viewer_home.content
    assert f"{reverse('project-list')}?scope=shared".encode() in viewer_home.content

    shared_home = viewer_client.get(reverse("home"), {"scope": "shared"})
    assert shared_home.status_code == 200
    assert reverse("project-view", args=[shared.id]).encode() in shared_home.content
    assert shared.name.encode() in shared_home.content
    assert private.name.encode() not in shared_home.content
    assert b'aria-current="page">Shared with me</a>' in shared_home.content


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
            {"package_files": [html, csv]},
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


def test_owner_can_publish_an_exact_revision_and_withdraw_it(
    owner: User,
    viewer: User,
    tmp_path: Path,
) -> None:
    project = Dashboard.objects.create(owner=owner, name="Publication workflow")
    revision = create_upload_revision(
        dashboard_id=project.id,
        created_by_id=owner.id,
        parts=[UploadPart("dashboard.html", [b"<html><body>Release</body></html>"], "text/html")],
        storage=FilesystemArtifactStorage(tmp_path / "publication"),
    )
    ViewerGrant.objects.create(dashboard=project, viewer=viewer, created_by=owner)
    owner_client = authenticated_client(owner)
    publish_url = reverse("project-publish", args=[project.id, revision.id])

    confirmation = owner_client.get(publish_url)
    assert confirmation.status_code == 200
    assert b"Publish revision 1?" in confirmation.content
    invalid = owner_client.post(publish_url, {"publication_note": "Release one"})
    assert invalid.status_code == 200
    project.refresh_from_db()
    assert project.state == Dashboard.State.DRAFT

    published = owner_client.post(
        publish_url,
        {"publication_note": "  Release one  ", "confirm": "on"},
    )
    assert published.status_code == 302
    project.refresh_from_db()
    assert project.state == Dashboard.State.PUBLISHED
    assert project.published_revision_id == revision.id
    assert project.publication_version == 1
    assert project.publication_note == "Release one"
    assert AuditEvent.objects.filter(
        event_type="dashboard.published",
        actor=owner,
        dashboard=project,
        revision=revision,
    ).exists()
    assert (
        authenticated_client(viewer).get(reverse("project-view", args=[project.id])).status_code
        == 200
    )

    unpublish_url = reverse("project-unpublish", args=[project.id])
    withdrawal = owner_client.get(unpublish_url)
    assert withdrawal.status_code == 200
    assert b"Withdraw revision 1?" in withdrawal.content
    withdrawn = owner_client.post(unpublish_url, {"confirm": "on"})
    assert withdrawn.status_code == 302
    withdrawn_project = Dashboard.objects.get(id=project.id)
    assert withdrawn_project.state == Dashboard.State.UNPUBLISHED
    assert withdrawn_project.published_revision_id is None
    assert withdrawn_project.publication_version == 1
    assert AuditEvent.objects.filter(
        event_type="dashboard.unpublished",
        actor=owner,
        dashboard=project,
        revision=revision,
        metadata={"publication_version": 1},
    ).exists()
    assert (
        authenticated_client(viewer).get(reverse("project-view", args=[project.id])).status_code
        == 404
    )


def test_publication_routes_are_owner_scoped_post_confirmed_and_csrf_protected(
    owner: User,
    viewer: User,
    tmp_path: Path,
) -> None:
    project = Dashboard.objects.create(owner=owner, name="Protected publication")
    revision = create_upload_revision(
        dashboard_id=project.id,
        created_by_id=owner.id,
        parts=[UploadPart("dashboard.html", [b"<html>Protected</html>"], "text/html")],
        storage=FilesystemArtifactStorage(tmp_path / "protected-publication"),
    )
    publish_url = reverse("project-publish", args=[project.id, revision.id])

    assert authenticated_client(viewer).get(publish_url).status_code == 404
    assert authenticated_client(owner).put(publish_url).status_code == 405

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert csrf_client.post(publish_url, {"confirm": "on"}).status_code == 403
    project.refresh_from_db()
    assert project.state == Dashboard.State.DRAFT


def test_upload_page_explains_flat_dashboard_package_contract(owner: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Upload guidance")
    response = authenticated_client(owner).get(reverse("project-upload", args=[project.id]))

    assert response.status_code == 200
    assert b'name="package_files"' in response.content
    assert b"data-upload-dropzone" in response.content
    assert b"Drag and drop dashboard files" in response.content
    assert b"replaces a queued file with the same name" in response.content
    assert b" multiple" in response.content
    assert b".html,.csv,.css,.png,.jpg,.jpeg,.gif,.webp,.woff,.woff2" in response.content
    assert (
        b"Relative references in the HTML and CSS must match selected filenames" in response.content
    )
    assert b"Outside URLs are blocked" in response.content
    assert b"separate JavaScript files are not supported" in response.content
    assert b"Revisions are immutable after upload" in response.content
    assert b"self-contained" not in response.content.lower()
    assert b"csv_files" not in response.content


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
            {"package_files": [html]},
        )

    assert response.status_code == 200
    assert b"The HTML uses a script src" in response.content
    assert b"the dashboard JavaScript must be inline" in response.content
    assert Revision.objects.filter(dashboard=project).exists() is False
    assert [path for path in (tmp_path / "private").rglob("*") if path.is_file()] == []


def test_anonymous_project_routes_redirect_to_login(owner: User) -> None:
    project = Dashboard.objects.create(owner=owner, name="Private")
    client = Client()
    for url in (
        reverse("legacy-project-list"),
        reverse("project-create"),
        reverse("project-detail", args=[project.id]),
        reverse("project-rename", args=[project.id]),
        reverse("project-upload", args=[project.id]),
        reverse(
            "project-preview",
            args=[project.id, "00000000-0000-0000-0000-000000000000"],
        ),
        reverse(
            "project-publish",
            args=[project.id, "00000000-0000-0000-0000-000000000000"],
        ),
        reverse("project-unpublish", args=[project.id]),
        reverse("project-view", args=[project.id]),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith(f"{reverse('login')}?next=")
