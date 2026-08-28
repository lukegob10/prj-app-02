from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import StreamingHttpResponse
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from agora.persistence.access import grant_project_viewer, revoke_project_viewer
from agora.persistence.models import (
    AuditEvent,
    Dashboard,
    ImmutableRecordError,
    RenderAuthorization,
    Revision,
    User,
    ViewerGrant,
)
from agora.persistence.storage import FilesystemArtifactStorage
from agora.persistence.uploads import create_upload_revision
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    RenderAuthorizationUnavailable,
    issue_owner_preview,
    issue_published_view,
    resolve_render_authorization,
    revoke_render_authorization,
)
from agora.rendering.views import render_csv
from agora.uploads import UploadPart

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_ORIGIN = "https://localhost:8443"
CONTENT_ORIGIN = "https://127.0.0.1:8444"
VALID_HTML = (
    b"<!doctype html><html><body><h1>Rendered dashboard</h1>"
    b'<script>fetch("data.csv")</script></body></html>'
)
VALID_CSV = b"name,value\nalpha,1\n"


def _user(soeid: str) -> User:
    return User.objects.create_user(soeid)


def _revision(owner: User, root: Path, *, name: str = "Dashboard") -> tuple[Dashboard, Revision]:
    dashboard = Dashboard.objects.create(owner=owner, name=name)
    revision = create_upload_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        parts=[
            UploadPart("dashboard.html", [VALID_HTML], "text/html"),
            UploadPart("data.csv", [VALID_CSV], "text/csv"),
        ],
        storage=FilesystemArtifactStorage(root),
    )
    dashboard.refresh_from_db()
    return dashboard, revision


def _publish(dashboard: Dashboard, revision: Revision) -> None:
    dashboard.published_revision = revision
    dashboard.first_published_at = timezone.now()
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.save()
    dashboard.refresh_from_db()


def _login(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _body(response: object) -> bytes:
    assert isinstance(response, StreamingHttpResponse)
    return b"".join(cast(Iterator[bytes], response.streaming_content))


def test_owner_preview_shell_issues_hashed_scoped_authorization(tmp_path: Path) -> None:
    owner = _user("RENDER.OWNER")
    outsider = _user("RENDER.OUTSIDER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    url = reverse("project-preview", args=[dashboard.id, revision.id])

    with override_settings(
        AGORA_ARTIFACT_ROOT=tmp_path / "artifacts",
        AGORA_CONTENT_ORIGIN=CONTENT_ORIGIN,
    ):
        response = _login(owner).get(url)

    assert response.status_code == 200
    match = re.search(
        rb"https://127\.0\.0\.1:8444/render/preview/([A-Za-z0-9_-]{43})/",
        response.content,
    )
    assert match is not None
    token = match.group(1).decode("ascii")
    authorization = RenderAuthorization.objects.get()
    assert authorization.token_digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert token not in authorization.token_digest
    assert authorization.viewer_id == owner.id
    assert authorization.dashboard_id == dashboard.id
    assert authorization.revision_id == revision.id
    assert authorization.audience == RenderAuthorization.Audience.PREVIEW
    assert b'sandbox="allow-scripts"' in response.content
    assert b'referrerpolicy="no-referrer"' in response.content
    assert b'<body class="portal-page portal-page--render">' in response.content
    assert b'class="portal-render-shell__bar"' in response.content
    assert b'class="portal-brand__wordmark"' in response.content
    assert b"/static/portal/brand/agora-wordmark-color.png" in response.content
    assert b"portal-brand__mark" not in response.content
    assert b'aria-label="Dashboard details"' in response.content
    assert b"Open project details" in response.content
    assert b"Manage" not in response.content
    assert response.content.count(b"<h1") == 1
    assert b"Private owner preview" in response.content
    assert AuditEvent.objects.filter(
        event_type="dashboard.preview_started",
        actor=owner,
        dashboard=dashboard,
        revision=revision,
    ).exists()

    assert _login(outsider).get(url).status_code == 404
    assert (
        _login(owner)
        .get(
            reverse("project-preview", args=[dashboard.id, "00000000-0000-0000-0000-000000000000"])
        )
        .status_code
        == 404
    )
    assert RenderAuthorization.objects.count() == 1


def test_content_entry_point_delivers_only_scoped_html_and_csv(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = _user("CONTENT.OWNER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    credential = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
    )
    client = Client()
    client.cookies["__Host-agora_session"] = "must-not-be-used"
    middleware = [
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.common.CommonMiddleware",
        "agora.middleware.ContentSecurityHeadersMiddleware",
    ]

    with caplog.at_level("WARNING", logger="django.request"):
        with override_settings(
            ROOT_URLCONF="agora.urls.content",
            MIDDLEWARE=middleware,
            AGORA_ARTIFACT_ROOT=tmp_path / "artifacts",
            AGORA_PORTAL_ORIGIN=PORTAL_ORIGIN,
            AGORA_CONTENT_ORIGIN=CONTENT_ORIGIN,
        ):
            html = client.get(f"/render/preview/{credential.token}/")
            csv = client.get(
                f"/render/preview/{credential.token}/data.csv",
                HTTP_ORIGIN="null",
            )
            foreign_origin_csv = client.get(
                f"/render/preview/{credential.token}/data.csv",
                HTTP_ORIGIN="https://evil.example",
            )
            head = client.head(f"/render/preview/{credential.token}/data.csv")
            missing = client.get(f"/render/preview/{credential.token}/missing.csv")
            wrong_audience = client.get(f"/render/viewer/{credential.token}/")
            altered = client.get(f"/render/preview/{credential.token[:-1]}A/")
            rejected_method = client.post(f"/render/preview/{credential.token}/")

    assert html.status_code == 200
    assert html["Content-Type"] == "text/html; charset=utf-8"
    assert html["Content-Length"] == str(len(VALID_HTML))
    assert _body(html) == VALID_HTML
    assert csv.status_code == 200
    assert csv["Content-Type"] == "text/csv; charset=utf-8"
    assert csv["Access-Control-Allow-Origin"] == "null"
    assert "Origin" in csv["Vary"]
    assert _body(csv) == VALID_CSV
    assert foreign_origin_csv.status_code == 200
    assert "Access-Control-Allow-Origin" not in foreign_origin_csv.headers
    assert head.status_code == 200
    assert head.content == b""
    assert head["Content-Length"] == str(len(VALID_CSV))
    assert missing.status_code == 404
    assert wrong_audience.status_code == 404
    assert altered.status_code == 404
    assert rejected_method.status_code == 405
    assert html["Cache-Control"] == "private, no-store"
    assert html["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors https://localhost:8443" in html["Content-Security-Policy"]
    assert "Set-Cookie" not in html.headers
    assert "X-Frame-Options" not in html.headers
    assert credential.token not in caplog.text
    assert "/render/<redacted>/" in caplog.text


def test_content_delivery_fails_closed_for_bad_names_and_missing_storage(tmp_path: Path) -> None:
    owner = _user("CONTENT.FAIL")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    credential = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
    )
    request = RequestFactory().get("/")
    invalid_name = render_csv(
        request,
        RenderAuthorization.Audience.PREVIEW,
        credential.token,
        "../data.csv",
    )
    assert invalid_name.status_code == 404
    assert request.path == "/render/<redacted>/"

    middleware = ["agora.middleware.ContentSecurityHeadersMiddleware"]
    with override_settings(
        ROOT_URLCONF="agora.urls.content",
        MIDDLEWARE=middleware,
        AGORA_ARTIFACT_ROOT=tmp_path / "empty-artifacts",
        AGORA_PORTAL_ORIGIN=PORTAL_ORIGIN,
    ):
        missing_bytes = Client().get(f"/render/preview/{credential.token}/")
    assert missing_bytes.status_code == 404


def test_preview_tokens_expire_revoke_and_follow_account_and_project_state(tmp_path: Path) -> None:
    owner = _user("TOKEN.OWNER")
    outsider = _user("TOKEN.OUTSIDER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    issued_at = timezone.now()

    with pytest.raises(RenderAuthorizationDenied):
        issue_owner_preview(
            dashboard_id=dashboard.id,
            revision_id=revision.id,
            viewer_id=outsider.id,
        )

    credential = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
        now=issued_at,
    )
    resolved = resolve_render_authorization(
        credential.token,
        audience=RenderAuthorization.Audience.PREVIEW,
        now=issued_at + timedelta(seconds=1),
    )
    assert resolved.dashboard.id == dashboard.id
    assert resolved.revision.id == revision.id
    assert resolved.viewer.id == owner.id

    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            credential.token,
            audience=RenderAuthorization.Audience.PREVIEW,
            now=issued_at + timedelta(minutes=6),
        )
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization("not-a-token", audience=RenderAuthorization.Audience.PREVIEW)

    authorization = RenderAuthorization.objects.get(
        token_digest=hashlib.sha256(credential.token.encode()).hexdigest()
    )
    revoke_render_authorization(authorization.id)
    revoke_render_authorization(authorization.id)
    revoke_render_authorization(UUID("00000000-0000-0000-0000-000000000000"))
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            credential.token,
            audience=RenderAuthorization.Audience.PREVIEW,
        )

    live_credential = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
    )
    dashboard.state = Dashboard.State.ARCHIVED
    dashboard.save()
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            live_credential.token,
            audience=RenderAuthorization.Audience.PREVIEW,
        )


def test_published_tokens_require_current_revision_and_active_grant(tmp_path: Path) -> None:
    owner = _user("PUBLISH.OWNER")
    viewer = _user("PUBLISH.VIEWER")
    outsider = _user("PUBLISH.OUTSIDER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    _publish(dashboard, revision)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)

    viewer_credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=viewer.id)
    owner_credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=owner.id)
    assert (
        resolve_render_authorization(
            viewer_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        ).revision.id
        == revision.id
    )
    assert (
        resolve_render_authorization(
            owner_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        ).viewer.id
        == owner.id
    )
    with pytest.raises(RenderAuthorizationDenied):
        issue_published_view(dashboard_id=dashboard.id, viewer_id=outsider.id)

    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save()
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            viewer_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        )

    dashboard.published_revision = None
    dashboard.state = Dashboard.State.UNPUBLISHED
    dashboard.save()
    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            owner_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        )
    with pytest.raises(RenderAuthorizationDenied):
        issue_published_view(dashboard_id=dashboard.id, viewer_id=owner.id)


@pytest.mark.parametrize("access_change", ["revoke", "disable", "unpublish"])
def test_viewer_html_and_csv_fail_on_the_next_check_after_access_changes(
    tmp_path: Path,
    access_change: str,
) -> None:
    owner = _user(f"BOUNDARY.{access_change}.OWNER")
    viewer = _user(f"BOUNDARY.{access_change}.VIEWER")
    dashboard, revision = _revision(owner, tmp_path / access_change)
    _publish(dashboard, revision)
    grant = ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=viewer.id)
    client = Client()
    middleware = ["agora.middleware.ContentSecurityHeadersMiddleware"]

    with override_settings(
        ROOT_URLCONF="agora.urls.content",
        MIDDLEWARE=middleware,
        AGORA_ARTIFACT_ROOT=tmp_path / access_change,
        AGORA_PORTAL_ORIGIN=PORTAL_ORIGIN,
    ):
        initial_html = client.get(f"/render/viewer/{credential.token}/")
        initial_csv = client.get(
            f"/render/viewer/{credential.token}/data.csv",
            HTTP_ORIGIN="null",
        )
        assert initial_html.status_code == 200
        assert _body(initial_html) == VALID_HTML
        assert initial_csv.status_code == 200
        assert _body(initial_csv) == VALID_CSV

        if access_change == "revoke":
            assert revoke_project_viewer(
                dashboard_id=dashboard.id,
                actor_id=owner.id,
                grant_id=grant.id,
            )
        elif access_change == "disable":
            viewer.is_active = False
            viewer.save(update_fields=("is_active",))
        else:
            dashboard.published_revision = None
            dashboard.state = Dashboard.State.UNPUBLISHED
            dashboard.save()

        denied_html = client.get(f"/render/viewer/{credential.token}/")
        denied_csv = client.get(
            f"/render/viewer/{credential.token}/data.csv",
            HTTP_ORIGIN="null",
        )

    assert denied_html.status_code == 404
    assert denied_csv.status_code == 404


def test_regrant_creates_new_access_without_reviving_old_render_credential(tmp_path: Path) -> None:
    owner = _user("EPOCH.OWNER")
    viewer = _user("EPOCH.VIEWER")
    dashboard, revision = _revision(owner, tmp_path / "epoch")
    _publish(dashboard, revision)
    first_grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    old_credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=viewer.id)
    old_authorization = RenderAuthorization.objects.get(
        token_digest=hashlib.sha256(old_credential.token.encode("ascii")).hexdigest()
    )
    assert old_authorization.viewer_grant_id == first_grant.id

    assert revoke_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        grant_id=first_grant.id,
    )
    second_grant = grant_project_viewer(
        dashboard_id=dashboard.id,
        actor_id=owner.id,
        target_soeid=viewer.soeid,
    )
    assert second_grant.id != first_grant.id

    with pytest.raises(RenderAuthorizationDenied):
        resolve_render_authorization(
            old_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        )

    new_credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=viewer.id)
    new_authorization = RenderAuthorization.objects.get(
        token_digest=hashlib.sha256(new_credential.token.encode("ascii")).hexdigest()
    )
    assert new_authorization.viewer_grant_id == second_grant.id
    assert (
        resolve_render_authorization(
            new_credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        ).viewer.id
        == viewer.id
    )


def test_render_token_issuance_does_not_lock_read_only_policy_rows(tmp_path: Path) -> None:
    owner = _user("NOLOCK.OWNER")
    viewer = _user("NOLOCK.VIEWER")
    dashboard, revision = _revision(owner, tmp_path / "no-lock")
    _publish(dashboard, revision)
    ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)

    with CaptureQueriesContext(connection) as queries:
        credential = issue_published_view(dashboard_id=dashboard.id, viewer_id=viewer.id)

    locking_queries = [
        query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"].upper()
    ]
    assert locking_queries == []

    with CaptureQueriesContext(connection) as resolver_queries:
        resolved = resolve_render_authorization(
            credential.token,
            audience=RenderAuthorization.Audience.VIEWER,
        )
    assert resolved.viewer.id == viewer.id
    assert len(resolver_queries) == 1


def test_stable_shared_view_shell_and_generic_denials(tmp_path: Path) -> None:
    owner = _user("SHELL.OWNER")
    viewer = _user("SHELL.VIEWER")
    outsider = _user("SHELL.OUTSIDER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    _publish(dashboard, revision)
    ViewerGrant.objects.create(dashboard=dashboard, viewer=viewer, created_by=owner)
    url = reverse("project-view", args=[dashboard.id])

    with override_settings(AGORA_CONTENT_ORIGIN=CONTENT_ORIGIN):
        viewer_response = _login(viewer).get(url)
        owner_response = _login(owner).get(url)
        outsider_response = _login(outsider).get(url)

    assert viewer_response.status_code == 200
    assert owner_response.status_code == 200
    assert b"Published dashboard" in viewer_response.content
    assert b"User-created content" in viewer_response.content
    assert b'aria-label="Dashboard details"' in viewer_response.content
    assert b"View project information" in viewer_response.content
    assert b"Open project details" in owner_response.content
    assert b"Manage" not in owner_response.content
    assert b"/render/viewer/" in viewer_response.content
    assert outsider_response.status_code == 404
    assert dashboard.name.encode() not in outsider_response.content


def test_authorization_records_are_scoped_immutable_tombstones(tmp_path: Path) -> None:
    owner = _user("MODEL.OWNER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    credential = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
    )
    authorization = RenderAuthorization.objects.get()
    assert "preview" in str(authorization)

    authorization.audience = RenderAuthorization.Audience.VIEWER
    with pytest.raises(ImmutableRecordError, match="scope is immutable"):
        authorization.save()
    authorization.refresh_from_db()
    with pytest.raises(ImmutableRecordError, match="expire or are revoked"):
        authorization.delete()

    authorization.viewer_auth_version += 1
    with pytest.raises(ImmutableRecordError):
        authorization.save()
    authorization.refresh_from_db()
    _, other_revision = _revision(owner, tmp_path / "other-artifacts", name="Other")
    authorization.revision = other_revision
    with pytest.raises((ImmutableRecordError, ValidationError)):
        authorization.full_clean()

    assert credential.token not in repr(authorization)


def test_token_collision_retries_then_fails_without_plaintext_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("COLLISION.OWNER")
    dashboard, revision = _revision(owner, tmp_path / "artifacts")
    token = "A" * 43
    monkeypatch.setattr("agora.rendering.authorization.secrets.token_urlsafe", lambda size: token)

    first = issue_owner_preview(
        dashboard_id=dashboard.id,
        revision_id=revision.id,
        viewer_id=owner.id,
    )
    assert first.token == token
    with pytest.raises(RenderAuthorizationUnavailable):
        issue_owner_preview(
            dashboard_id=dashboard.id,
            revision_id=revision.id,
            viewer_id=owner.id,
        )
    assert RenderAuthorization.objects.count() == 1
