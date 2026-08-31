"""Trusted portal views for projects, access management, and identity workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login, logout
from django.contrib.auth.decorators import login_required
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from agora.persistence.access import (
    GrantRejection,
    GrantViewerRejected,
    ProjectAccessDenied,
    grant_project_viewer,
    revoke_project_viewer,
)
from agora.persistence.authentication import (
    DuplicateSoeid,
    LastAdministratorError,
    NotAdministrator,
    PasswordPolicyError,
    SelfDisableError,
    UserNotFound,
    authenticate_login,
    disable_user,
    enable_user,
    provision_user,
    record_logout,
    reset_user_password,
)
from agora.persistence.enhancements import EnhancementAccessDenied, request_dashboard_access
from agora.persistence.models import Dashboard, DashboardFavorite, Revision, User
from agora.persistence.pagination import (
    CursorColumn,
    CursorPage,
    CursorValueKind,
    InvalidCursor,
    paginate_keyset,
)
from agora.persistence.projects import (
    ProjectOwnerUnavailable,
    ProjectRenameDenied,
    create_project,
    manageable_project,
    prefetch_revision_artifacts,
    project_active_grants,
    project_grant_epoch,
    project_grant_history,
    project_revisions,
    rename_project,
    visible_project,
)
from agora.persistence.querying import administrator_user_list
from agora.persistence.services import RevisionCreationError
from agora.persistence.storage import ArtifactStorageError, FilesystemArtifactStorage
from agora.persistence.uploads import create_upload_revision
from agora.rendering.authorization import (
    RenderAuthorizationDenied,
    RenderAuthorizationUnavailable,
    RenderCredential,
    issue_owner_preview,
    issue_published_view,
)
from agora.rendering.security import portal_content_iframe_attributes
from agora.uploads import (
    UploadIssue,
    UploadIssueCode,
    UploadIssueField,
    UploadLimits,
    UploadPart,
    UploadRejected,
)

from .forms import (
    ConfirmActionForm,
    GrantViewerForm,
    LoginForm,
    ProjectForm,
    ProjectRenameForm,
    ProvisionUserForm,
    ResetPasswordForm,
    RevisionUploadForm,
    UserSearchForm,
)
from .security import administrator_required, safe_next_url
from .stewardship_forms import DashboardAccessRequestForm

logger = logging.getLogger(__name__)

GENERIC_LOGIN_ERROR = "Sign-in failed. Check your SOEID and password."
UPLOAD_MESSAGES = {
    UploadIssueCode.MALFORMED_MULTIPART: "The upload form was incomplete. Choose the files again.",
    UploadIssueCode.TOO_MANY_FILES: (
        "Too many files were selected. Upload one HTML entry point and up to 50 supporting files."
    ),
    UploadIssueCode.MISSING_HTML: "Choose one dashboard HTML entry point.",
    UploadIssueCode.MULTIPLE_HTML: "Choose exactly one dashboard HTML entry point.",
    UploadIssueCode.INVALID_FILENAME: "A selected file has an unsupported filename.",
    UploadIssueCode.DUPLICATE_FILENAME: "Every selected file must have a unique filename.",
    UploadIssueCode.EXTENSION_MISMATCH: (
        "Only .html, .csv, .css, .png, .jpg, .jpeg, .gif, .webp, .woff, and .woff2 "
        "files are accepted."
    ),
    UploadIssueCode.MEDIA_TYPE_MISMATCH: "A selected file does not match its declared file type.",
    UploadIssueCode.EMPTY_FILE: "Empty files cannot be uploaded.",
    UploadIssueCode.FILE_TOO_LARGE: "A selected file exceeds the 25 MB limit.",
    UploadIssueCode.TOTAL_TOO_LARGE: "The combined upload exceeds the 100 MB limit.",
    UploadIssueCode.INVALID_UTF8: "HTML, CSV, and CSS files must use UTF-8 text encoding.",
    UploadIssueCode.BINARY_CONTENT: "Binary content is not accepted in HTML, CSV, or CSS files.",
    UploadIssueCode.CSV_MALFORMED: "A CSV supporting file is malformed.",
    UploadIssueCode.HTML_MALFORMED: "The dashboard HTML entry point is malformed.",
    UploadIssueCode.HTML_TOO_COMPLEX: (
        "The dashboard HTML entry point exceeds the supported complexity limits."
    ),
    UploadIssueCode.EXTERNAL_DEPENDENCY: (
        "Outside URLs and network dependencies are blocked. Reference only selected supporting "
        "filenames, and keep JavaScript inline; separate JS files are not supported."
    ),
    UploadIssueCode.INVALID_CSV_REFERENCE: "The dashboard contains an invalid CSV reference.",
    UploadIssueCode.MISSING_CSV_REFERENCE: (
        "The dashboard references a CSV file that was not selected."
    ),
    UploadIssueCode.INVALID_ASSET_REFERENCE: (
        "The dashboard contains an invalid supporting-file reference."
    ),
    UploadIssueCode.MISSING_ASSET_REFERENCE: (
        "The dashboard references a supporting file that was not selected."
    ),
}
UPLOAD_EXTERNAL_DEPENDENCY_MESSAGES = {
    UploadIssueField.CSS_IMPORT: (
        "A stylesheet uses @import. Select that stylesheet directly and link it from the HTML "
        "instead."
    ),
    UploadIssueField.CSS_EXTERNAL_URL: (
        "A stylesheet loads an outside URL. Select the referenced image or web font and use its "
        "package-local filename instead."
    ),
    UploadIssueField.HTML_PROCESSING_INSTRUCTION: (
        "The HTML contains a processing instruction, which dashboard packages do not support."
    ),
    UploadIssueField.HTML_EMBEDDED_CONTENT: (
        "The HTML embeds another document or plugin. Iframes, objects, embeds, frames, portals, "
        "and base URLs are not supported."
    ),
    UploadIssueField.HTML_LINK: (
        "The HTML contains an unsupported link element. Link only a selected local stylesheet "
        "or icon file."
    ),
    UploadIssueField.HTML_SCRIPT_SOURCE: (
        "The HTML uses a script src. External and separate JavaScript files are not supported; "
        "the dashboard JavaScript must be inline."
    ),
    UploadIssueField.HTML_MANIFEST: (
        "The HTML references an application manifest, which is blocked."
    ),
    UploadIssueField.HTML_FORM_ACTION: (
        "The HTML contains a form or form submission target. Dashboard form submissions are "
        "blocked."
    ),
    UploadIssueField.HTML_RESPONSIVE_SOURCE: (
        "The HTML uses srcset or imagesrcset. Select one local image and reference its exact "
        "filename with src instead."
    ),
    UploadIssueField.HTML_META_URL: (
        "HTML metadata contains an outside URL. Remove that network reference from the metadata."
    ),
    UploadIssueField.HTML_META_REFRESH: "HTML meta refresh navigation is blocked.",
    UploadIssueField.INLINE_SCRIPT_URL: (
        "Inline JavaScript contains an outside URL. Load data from an exact selected CSV filename "
        'instead, for example fetch("sales.csv").'
    ),
    UploadIssueField.INLINE_NETWORK_API: (
        "Inline JavaScript uses a blocked networking, worker, or service-worker API. Load a "
        "selected CSV with a literal local fetch or d3.csv call instead."
    ),
    UploadIssueField.DYNAMIC_DATA_REFERENCE: (
        "A fetch or d3.csv call does not use one literal selected CSV filename. Use a call such "
        'as fetch("sales.csv") or d3.csv("sales.csv").'
    ),
    UploadIssueField.DATA_TEMPLATE_LITERAL: (
        "A fetch or d3.csv call contains an incomplete backtick template. Close the template "
        "before the call's remaining arguments."
    ),
    UploadIssueField.DATA_IDENTIFIER: (
        "A fetch or d3.csv call passes a variable instead of a provably local CSV filename. "
        'Pass the exact selected filename directly, such as fetch("sales.csv").'
    ),
    UploadIssueField.DATA_EXPRESSION: (
        "A fetch or d3.csv call builds its location dynamically. Pass one exact selected CSV "
        "filename directly instead."
    ),
    UploadIssueField.EXTERNAL_REFERENCE: (
        "An HTML resource points outside the selected package. Select that supported resource "
        "and reference its exact local filename."
    ),
}

REVISION_CURSOR_COLUMNS = (CursorColumn("number", CursorValueKind.INTEGER, descending=True),)
ACTIVE_GRANT_CURSOR_COLUMNS = (CursorColumn("viewer__soeid", CursorValueKind.TEXT),)
GRANT_HISTORY_CURSOR_COLUMNS = (
    CursorColumn("revoked_at", CursorValueKind.DATETIME, descending=True),
    CursorColumn("id", CursorValueKind.UUID, descending=True),
)
USER_CURSOR_COLUMNS = (CursorColumn("soeid", CursorValueKind.TEXT),)
PROJECT_PAGE_SIZE = 25
REVISION_PAGE_SIZE = 25
GRANT_PAGE_SIZE = 25
USER_PAGE_SIZE = 25
RENDER_ARTIFACT_LIMIT = UploadLimits().max_files
_UPLOAD_FILE_FIELDS = frozenset(
    {"package_files", "files", "html_file", "supporting_files", "csv_files"}
)

GRANT_REJECTION_MESSAGES = {
    GrantRejection.INVALID_SOEID: "Enter a valid canonical SOEID.",
    GrantRejection.UNKNOWN_USER: "No active user was found for that SOEID.",
    GrantRejection.DISABLED_USER: "That account is disabled and cannot receive Viewer access.",
    GrantRejection.SELF_GRANT: (
        "You cannot grant yourself access; owners already have Full control."
    ),
    GrantRejection.ALREADY_GRANTED: "That SOEID already has Viewer access to this project.",
}


def home(request: HttpRequest) -> HttpResponse:
    """Render the public landing or unified authenticated Projects screen."""
    from .discovery_views import home as discovery_home

    return discovery_home(request, page_size=PROJECT_PAGE_SIZE)


@login_required
@require_http_methods(["GET"])
def project_list(request: HttpRequest) -> HttpResponse:
    """Compatibility wrapper preserving the established pagination test seam."""
    from .discovery_views import project_list as discovery_project_list

    return discovery_project_list(request, page_size=PROJECT_PAGE_SIZE)


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request: HttpRequest) -> HttpResponse:
    """Create one private owner project from safe metadata fields only."""
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            project = create_project(
                owner_id=cast(User, request.user).id,
                name=cast(str, form.cleaned_data["name"]),
                description=cast(str, form.cleaned_data["description"]),
            )
        except ProjectOwnerUnavailable:
            return render(request, "portal/forbidden.html", status=403)
        messages.success(request, "Project created. Add a dashboard revision when you are ready.")
        return redirect("project-detail", project_id=project.id)
    return render(request, "portal/projects/create.html", {"form": form})


@login_required
@require_http_methods(["GET"])
def project_detail(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Show owner controls or the narrow published view granted to a viewer."""
    user = cast(User, request.user)
    resolved = visible_project(project_id, user.id)
    if resolved is None:
        return render(request, "portal/not_found.html", status=404)
    project, is_owner = resolved
    revision_page: CursorPage[Revision] | None = None
    revisions: tuple[Revision, ...] = ()
    if is_owner:
        try:
            revision_page = paginate_keyset(
                project_revisions(project.id, user.id),
                columns=REVISION_CURSOR_COLUMNS,
                namespace="project-revisions",
                context=f"{project.id}:{user.id}",
                cursor=request.GET.get("cursor"),
                page_size=REVISION_PAGE_SIZE,
            )
        except InvalidCursor:
            return render(request, "portal/not_found.html", status=404)
        prefetch_revision_artifacts(revision_page.items)
        revision_page = revision_page.with_urls(
            base_url=reverse("project-detail", args=[project.id]),
            cursor_parameter="cursor",
        )
        revisions = revision_page.items
    elif request.GET.get("cursor"):
        return render(request, "portal/not_found.html", status=404)
    return render(
        request,
        "portal/projects/detail.html",
        {
            "project": project,
            "can_rename": is_owner and project.state != Dashboard.State.ARCHIVED,
            "is_owner": is_owner,
            "revisions": revisions,
            "revision_page": revision_page,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_rename(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Rename one active dashboard through its current owner boundary."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None or project.state == Dashboard.State.ARCHIVED:
        return render(request, "portal/not_found.html", status=404)

    form = ProjectRenameForm(
        request.POST if request.method == "POST" else None,
        initial={"name": project.name},
    )
    if request.method == "POST" and form.is_valid():
        try:
            rename_project(
                project_id=project.id,
                owner_id=user.id,
                name=cast(str, form.cleaned_data["name"]),
            )
        except ProjectRenameDenied:
            return render(request, "portal/not_found.html", status=404)
        messages.success(request, "Dashboard renamed.")
        return redirect("project-detail", project_id=project.id)

    return render(
        request,
        "portal/projects/rename.html",
        {"form": form, "project": project},
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_access(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """List and manage Viewer epochs for one owner-controlled project."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return render(request, "portal/not_found.html", status=404)
    access_url = reverse("project-access", args=[project.id])

    form = GrantViewerForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        target_soeid = cast(str, form.cleaned_data["soeid"])
        try:
            grant_project_viewer(
                dashboard_id=project.id,
                actor_id=user.id,
                target_soeid=target_soeid,
            )
        except GrantViewerRejected as error:
            form.add_error("soeid", _grant_rejection_message(error))
        except ProjectAccessDenied:
            return render(request, "portal/not_found.html", status=404)
        else:
            messages.success(request, f"Viewer access granted to {target_soeid}.")
            return redirect("project-access", project_id=project.id)

    if form.errors:
        form.fields["soeid"].widget.attrs["aria-describedby"] = (
            "viewer-soeid-help viewer-soeid-error"
        )
        form.fields["soeid"].widget.attrs["aria-invalid"] = "true"

    active_cursor = request.GET.get("active_cursor")
    history_cursor = request.GET.get("history_cursor")
    try:
        active_page = paginate_keyset(
            project_active_grants(project.id, user.id),
            columns=ACTIVE_GRANT_CURSOR_COLUMNS,
            namespace="project-active-grants",
            context=f"{project.id}:{user.id}",
            cursor=active_cursor,
            page_size=GRANT_PAGE_SIZE,
        )
        history_page = paginate_keyset(
            project_grant_history(project.id, user.id),
            columns=GRANT_HISTORY_CURSOR_COLUMNS,
            namespace="project-grant-history",
            context=f"{project.id}:{user.id}",
            cursor=history_cursor,
            page_size=GRANT_PAGE_SIZE,
        )
    except InvalidCursor:
        return render(request, "portal/not_found.html", status=404)

    active_page = active_page.with_urls(
        base_url=access_url,
        cursor_parameter="active_cursor",
        preserved_query={"history_cursor": history_cursor or ""},
    )
    history_page = history_page.with_urls(
        base_url=access_url,
        cursor_parameter="history_cursor",
        preserved_query={"active_cursor": active_cursor or ""},
    )
    return render(
        request,
        "portal/projects/access.html",
        {
            "project": project,
            "owner_soeid": user.soeid,
            "stable_view_url": (
                f"{settings.AGORA_PORTAL_ORIGIN.rstrip('/')}"
                f"{reverse('project-view', args=[project.id])}"
            ),
            "effective_access_page_count": sum(
                1 for grant in active_page.items if grant.viewer.is_active
            ),
            "form": form,
            "grant_url": access_url,
            "active_grants": active_page.items,
            "active_grants_page": active_page,
            "grant_history": history_page.items,
            "grant_history_page": history_page,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_grant_revoke(
    request: HttpRequest,
    project_id: UUID,
    grant_id: UUID,
) -> HttpResponse:
    """Confirm and perform an idempotent revocation of one Viewer epoch."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return render(request, "portal/not_found.html", status=404)

    form = ConfirmActionForm(request.POST if request.method == "POST" else None)
    form.fields["confirm"].label = "I understand this ends Viewer access for this project."
    if request.method == "POST" and form.is_valid():
        try:
            revoked_grant = revoke_project_viewer(
                dashboard_id=project.id,
                grant_id=grant_id,
                actor_id=user.id,
            )
        except ProjectAccessDenied:
            return render(request, "portal/not_found.html", status=404)
        messages.success(request, f"Viewer access for {revoked_grant.viewer.soeid} was revoked.")
        return redirect("project-access", project_id=project.id)

    if form.errors:
        form.fields["confirm"].widget.attrs["aria-describedby"] = "revoke-confirm-error"
        form.fields["confirm"].widget.attrs["aria-invalid"] = "true"

    grant = project_grant_epoch(project.id, user.id, grant_id)
    if grant is None:
        return render(request, "portal/not_found.html", status=404)

    return render(
        request,
        "portal/projects/revoke.html",
        {
            "project": project,
            "grant": grant,
            "form": form,
        },
    )


@login_required
@require_http_methods(["GET"])
def project_preview(
    request: HttpRequest,
    project_id: UUID,
    revision_id: UUID,
) -> HttpResponse:
    """Frame an exact owner revision with a new short-lived content credential."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return render(request, "portal/not_found.html", status=404)
    revision = project_revisions(project.id, user.id).filter(id=revision_id).first()
    if revision is None:
        return render(request, "portal/not_found.html", status=404)
    try:
        credential = issue_owner_preview(
            dashboard_id=project.id,
            revision_id=revision.id,
            viewer_id=user.id,
        )
    except RenderAuthorizationDenied:
        return render(request, "portal/not_found.html", status=404)
    except RenderAuthorizationUnavailable:
        return render(request, "portal/render_unavailable.html", status=503)
    return _render_dashboard_shell(
        request,
        project=project,
        revision=revision,
        credential=credential,
        is_preview=True,
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_view(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Resolve an authorized stable URL or offer the same generic request treatment."""
    user = cast(User, request.user)
    if request.method == "POST":
        request_form = DashboardAccessRequestForm(request.POST)
        if request_form.is_valid():
            try:
                request_dashboard_access(
                    dashboard_id=project_id,
                    requester_id=user.id,
                    message=cast(str, request_form.cleaned_data["message"]),
                )
            except EnhancementAccessDenied:
                # Invalid, hidden, ineligible, and already-authorized identifiers deliberately
                # receive the same acknowledgement as an eligible request.
                pass
        return render(
            request,
            "portal/projects/request_access.html",
            {
                "request_form": DashboardAccessRequestForm(),
                "request_url": request.path,
                "request_submitted": True,
            },
        )

    resolved = visible_project(project_id, user.id)
    if resolved is None:
        return _render_access_request(request, status=404)
    project, _ = resolved
    if project.published_revision is None:
        return _render_access_request(request, status=404)
    try:
        credential = issue_published_view(dashboard_id=project.id, viewer_id=user.id)
    except RenderAuthorizationDenied:
        return render(request, "portal/not_found.html", status=404)
    except RenderAuthorizationUnavailable:
        return render(request, "portal/render_unavailable.html", status=503)
    return _render_dashboard_shell(
        request,
        project=project,
        revision=project.published_revision,
        credential=credential,
        is_preview=False,
    )


def _render_access_request(request: HttpRequest, *, status: int) -> HttpResponse:
    """Render the metadata-free stable-link fallback without exposing target validity."""

    return render(
        request,
        "portal/projects/request_access.html",
        {
            "request_form": DashboardAccessRequestForm(),
            "request_url": request.path,
            "request_submitted": False,
        },
        status=status,
    )


def _render_dashboard_shell(
    request: HttpRequest,
    *,
    project: Dashboard,
    revision: Revision,
    credential: RenderCredential,
    is_preview: bool,
) -> HttpResponse:
    content_url = (
        f"{settings.AGORA_CONTENT_ORIGIN}/render/{credential.audience}/{credential.token}/"
    )
    iframe = portal_content_iframe_attributes(
        content_url,
        content_origin=settings.AGORA_CONTENT_ORIGIN,
    )
    return render(
        request,
        "portal/projects/render.html",
        {
            "project": project,
            "revision": revision,
            "artifacts": list(
                revision.artifacts.only(
                    "id",
                    "revision",
                    "kind",
                    "logical_name",
                    "byte_size",
                ).order_by("kind", "logical_name", "id")[:RENDER_ARTIFACT_LIMIT]
            ),
            "is_preview": is_preview,
            "is_favorite": (
                not is_preview
                and DashboardFavorite.objects.filter(
                    dashboard_id=project.id,
                    user_id=cast(User, request.user).id,
                ).exists()
            ),
            "expires_at": credential.expires_at,
            "iframe_src": iframe["src"],
            "iframe_sandbox": iframe["sandbox"],
            "iframe_referrerpolicy": iframe["referrerpolicy"],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_upload(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Validate and persist one immutable owner revision from multipart files."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return render(request, "portal/not_found.html", status=404)

    form = RevisionUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        storage = FilesystemArtifactStorage(Path(settings.AGORA_ARTIFACT_ROOT))
        try:
            parts = _request_upload_parts(request)
            revision = create_upload_revision(
                dashboard_id=project.id,
                created_by_id=user.id,
                parts=parts,
                storage=storage,
            )
        except UploadRejected as error:
            logger.warning(
                "Dashboard upload rejected: code=%s part_index=%s field=%s dashboard_id=%s",
                error.issue.code.value,
                error.issue.part_index,
                error.issue.field.value if error.issue.field is not None else "unspecified",
                project.id,
            )
            form.add_error(None, _upload_error_message(error))
        except ArtifactStorageError, RevisionCreationError:
            form.add_error(
                None,
                "The upload could not be stored safely. No revision was created; try again.",
            )
        else:
            messages.success(request, f"Revision {revision.number} uploaded successfully.")
            return redirect("project-detail", project_id=project.id)
    return render(request, "portal/projects/upload.html", {"form": form, "project": project})


def _request_upload_parts(request: HttpRequest) -> list[UploadPart]:
    """Return every accepted multipart file or reject the request shape.

    The current UI submits one ordered ``package_files`` list. Former unified and split field
    names remain accepted during rollout, but mixing shapes, adding unknown fields, or hiding
    repeated values cannot bypass the package validator's count and size limits.
    """

    file_fields = set(request.FILES)
    if not file_fields <= _UPLOAD_FILE_FIELDS:
        raise UploadRejected(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART))

    selected_files: list[UploadedFile[Any]] = request.FILES.getlist("package_files")
    former_unified_files: list[UploadedFile[Any]] = request.FILES.getlist("files")
    html_files: list[UploadedFile[Any]] = request.FILES.getlist("html_file")
    supporting_files: list[UploadedFile[Any]] = request.FILES.getlist("supporting_files")
    legacy_csv_files: list[UploadedFile[Any]] = request.FILES.getlist("csv_files")
    legacy_files = [*former_unified_files, *html_files, *supporting_files, *legacy_csv_files]
    if selected_files:
        if legacy_files:
            raise UploadRejected(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART))
        return [_upload_part(item) for item in selected_files]

    if former_unified_files:
        if html_files or supporting_files or legacy_csv_files:
            raise UploadRejected(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART))
        return [_upload_part(item) for item in former_unified_files]

    if len(html_files) != 1:
        code = UploadIssueCode.MISSING_HTML if not html_files else UploadIssueCode.MULTIPLE_HTML
        raise UploadRejected(UploadIssue(code))

    if supporting_files and legacy_csv_files:
        raise UploadRejected(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART))
    # Accept the former CSV-only multipart key during rollout without exposing a second field
    # in the rendered form. New clients always submit ``supporting_files``.
    selected_supporting_files = supporting_files or legacy_csv_files
    return [
        _upload_part(html_files[0]),
        *(_upload_part(item) for item in selected_supporting_files),
    ]


def _upload_part(uploaded: UploadedFile[Any]) -> UploadPart:
    return UploadPart(
        filename=uploaded.name or "",
        chunks=uploaded.chunks,
        media_type=uploaded.content_type,
        content_length=uploaded.size,
    )


def _upload_error_message(error: UploadRejected) -> str:
    message = UPLOAD_MESSAGES.get(
        error.issue.code,
        "The upload could not be accepted. Review the selected files and try again.",
    )
    if error.issue.code == UploadIssueCode.EXTERNAL_DEPENDENCY and error.issue.field is not None:
        message = UPLOAD_EXTERNAL_DEPENDENCY_MESSAGES.get(error.issue.field, message)
    if error.issue.part_index is None:
        return message
    return f"Problem with a selected file: {message}"


def _grant_rejection_message(error: GrantViewerRejected) -> str:
    """Translate typed domain outcomes into safe, actionable form copy."""
    return GRANT_REJECTION_MESSAGES.get(
        error.reason,
        "Viewer access could not be granted. Review the SOEID and try again.",
    )


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """Authenticate only the canonical SOEID supplied by the portal form."""
    current_user = getattr(request, "user", None)
    candidate = request.POST.get(REDIRECT_FIELD_NAME) if request.method == "POST" else None
    if candidate is None:
        candidate = request.GET.get(REDIRECT_FIELD_NAME)
    next_url = safe_next_url(request, candidate)

    if current_user is not None and current_user.is_authenticated:
        return redirect(next_url)

    form = LoginForm(request.POST or None, initial={REDIRECT_FIELD_NAME: next_url})
    if request.method == "POST":
        if form.is_valid():
            user = authenticate_login(
                request,
                cast(str, form.cleaned_data["soeid"]),
                cast(str, form.cleaned_data["password"]),
            )
            if user is not None:
                login(request, user)
                return redirect(next_url)
        else:
            # Count malformed submissions too, without retaining or displaying their values.
            authenticate_login(
                request,
                str(request.POST.get("soeid", "")),
                str(request.POST.get("password", "")),
            )
        form = LoginForm(initial={REDIRECT_FIELD_NAME: next_url})

    return render(
        request,
        "portal/login.html",
        {
            "form": form,
            "hide_account_menu": True,
            "login_error": GENERIC_LOGIN_ERROR if request.method == "POST" else None,
        },
    )


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    """Flush the portal session only through a CSRF-protected POST."""
    user = getattr(request, "user", None)
    if isinstance(user, User):
        record_logout(user)
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


@administrator_required
@require_http_methods(["GET"])
def user_list(request: HttpRequest) -> HttpResponse:
    """List a bounded identity page, optionally narrowed by canonical SOEID prefix."""

    user = cast(User, request.user)
    search_form = UserSearchForm({"query": str(request.GET.get("query", ""))[:65]})
    cursor = request.GET.get("cursor")
    user_page: CursorPage[User]
    if not search_form.is_valid():
        if cursor:
            return render(request, "portal/not_found.html", status=404)
        user_page = CursorPage(items=(), previous_cursor=None, next_cursor=None)
        search_query = ""
    else:
        search_query = cast(str, search_form.cleaned_data["query"])
        try:
            user_page = paginate_keyset(
                administrator_user_list(soeid_prefix=search_query),
                columns=USER_CURSOR_COLUMNS,
                namespace="administrator-user-list",
                context=f"{user.id}:{search_query}",
                cursor=cursor,
                page_size=USER_PAGE_SIZE,
            )
        except InvalidCursor:
            return render(request, "portal/not_found.html", status=404)
        user_page = user_page.with_urls(
            base_url=reverse("admin-user-list"),
            cursor_parameter="cursor",
            preserved_query={"query": search_query},
        )

    list_url = reverse("admin-user-list")
    return render(
        request,
        "portal/admin/user_list.html",
        {
            "users": user_page.items,
            "user_page": user_page,
            "search_form": search_form,
            "search_query": search_query,
            "search_url": list_url,
            "clear_search_url": list_url,
        },
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def user_create(request: HttpRequest) -> HttpResponse:
    """Provision one account through an explicit administrator-only form."""
    form = ProvisionUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            provision_user(
                actor_id=cast(UUID, request.user.id),
                soeid=cast(str, form.cleaned_data["soeid"]),
                password=cast(str, form.cleaned_data["password"]),
                is_administrator=bool(form.cleaned_data["is_administrator"]),
            )
        except DuplicateSoeid:
            form.add_error("soeid", "A user with that SOEID already exists.")
        except PasswordPolicyError as error:
            for message in error.messages:
                form.add_error("password", message)
        except NotAdministrator:
            return render(request, "portal/forbidden.html", status=403)
        else:
            messages.success(
                request,
                "User created. Deliver the initial password through an approved secure channel.",
            )
            return redirect("admin-user-list")
    return render(request, "portal/admin/user_form.html", {"form": form, "mode": "create"})


@administrator_required
@require_http_methods(["GET", "POST"])
def user_disable(request: HttpRequest, user_id: UUID) -> HttpResponse:
    """Require explicit confirmation before disabling an account."""
    target = get_object_or_404(
        User.objects.only("id", "soeid", "is_active", "is_administrator"), id=user_id
    )
    was_active = target.is_active
    form = ConfirmActionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            disable_user(
                actor_id=cast(UUID, request.user.id),
                target_id=target.id,
            )
        except LastAdministratorError:
            form.add_error(None, "The last active administrator cannot be disabled.")
        except SelfDisableError:
            form.add_error(None, "Use another administrator account to disable this account.")
        except (NotAdministrator, UserNotFound):  # fmt: skip
            return render(request, "portal/forbidden.html", status=403)
        else:
            if was_active:
                messages.success(request, "The user was disabled and active sessions were revoked.")
            else:
                messages.info(request, "That user is already disabled.")
            return redirect("admin-user-list")
    return render(
        request,
        "portal/admin/user_confirm_disable.html",
        {"form": form, "target": target},
    )


@administrator_required
@require_POST
def user_enable(request: HttpRequest, user_id: UUID) -> HttpResponse:
    """Re-enable a retained account without changing its credentials."""
    target = get_object_or_404(User.objects.only("id", "soeid", "is_active"), id=user_id)
    try:
        enable_user(
            actor_id=cast(UUID, request.user.id),
            target_id=target.id,
        )
    except (NotAdministrator, UserNotFound):  # fmt: skip
        return render(request, "portal/forbidden.html", status=403)
    messages.success(request, "The user is active. Reset the password if credentials are unknown.")
    return redirect("admin-user-list")


@administrator_required
@require_http_methods(["GET", "POST"])
def user_reset_password(request: HttpRequest, user_id: UUID) -> HttpResponse:
    """Replace a password without ever rendering the submitted value."""
    target = get_object_or_404(User.objects.only("id", "soeid", "is_active"), id=user_id)
    form = ResetPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reset = reset_user_password(
                actor_id=cast(UUID, request.user.id),
                target_id=target.id,
                password=cast(str, form.cleaned_data["password"]),
            )
        except PasswordPolicyError as error:
            for message in error.messages:
                form.add_error("password", message)
        except (NotAdministrator, UserNotFound):  # fmt: skip
            return render(request, "portal/forbidden.html", status=403)
        else:
            if reset.id == request.user.id:
                request.session.cycle_key()
                request.session["_auth_user_hash"] = reset.get_session_auth_hash()
            messages.success(request, "The password was reset. It will not be displayed again.")
            return redirect("admin-user-list")
    return render(
        request,
        "portal/admin/user_reset_password.html",
        {"form": form, "target": target},
    )
