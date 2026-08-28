"""Trusted portal views for projects, access management, and identity workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login, logout
from django.contrib.auth.decorators import login_required
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
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
from agora.persistence.models import Dashboard, Revision, User
from agora.persistence.projects import (
    ProjectOwnerUnavailable,
    create_project,
    manageable_project,
    owned_projects,
    project_active_grants,
    project_effective_viewer_count,
    project_grant_history,
    project_revisions,
    shared_projects,
    visible_project,
)
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
from agora.uploads import UploadIssueCode, UploadPart, UploadRejected

from .forms import (
    ConfirmActionForm,
    GrantViewerForm,
    LoginForm,
    ProjectForm,
    ProvisionUserForm,
    ResetPasswordForm,
    RevisionUploadForm,
)
from .security import administrator_required, safe_next_url

GENERIC_LOGIN_ERROR = "Sign-in failed. Check your SOEID and password."
UPLOAD_MESSAGES = {
    UploadIssueCode.MALFORMED_MULTIPART: "The upload form was incomplete. Choose the files again.",
    UploadIssueCode.TOO_MANY_FILES: (
        "Too many files were selected. Upload one HTML and up to 50 CSV files."
    ),
    UploadIssueCode.MISSING_HTML: "Choose one dashboard HTML file.",
    UploadIssueCode.MULTIPLE_HTML: "Choose exactly one dashboard HTML file.",
    UploadIssueCode.INVALID_FILENAME: "A selected file has an unsupported filename.",
    UploadIssueCode.DUPLICATE_FILENAME: "Every selected file must have a unique filename.",
    UploadIssueCode.EXTENSION_MISMATCH: "Only .html and .csv files are accepted.",
    UploadIssueCode.MEDIA_TYPE_MISMATCH: "A selected file does not match its declared file type.",
    UploadIssueCode.EMPTY_FILE: "Empty files cannot be uploaded.",
    UploadIssueCode.FILE_TOO_LARGE: "A selected file exceeds the 25 MB limit.",
    UploadIssueCode.TOTAL_TOO_LARGE: "The combined upload exceeds the 100 MB limit.",
    UploadIssueCode.INVALID_UTF8: "Dashboard and CSV files must use UTF-8 text encoding.",
    UploadIssueCode.BINARY_CONTENT: "Binary content is not accepted in dashboard or CSV files.",
    UploadIssueCode.CSV_MALFORMED: "A CSV attachment is malformed.",
    UploadIssueCode.HTML_MALFORMED: "The dashboard HTML is malformed.",
    UploadIssueCode.HTML_TOO_COMPLEX: "The dashboard HTML exceeds the supported complexity limits.",
    UploadIssueCode.EXTERNAL_DEPENDENCY: (
        "The dashboard must be self-contained without external resources. Embed scripts and "
        "styles, and present external source URLs as plain text."
    ),
    UploadIssueCode.INVALID_CSV_REFERENCE: "The dashboard contains an invalid CSV reference.",
    UploadIssueCode.MISSING_CSV_REFERENCE: (
        "The dashboard references a CSV file that was not attached."
    ),
}

PROJECT_PAGE_SIZE = 25
REVISION_PAGE_SIZE = 20
GRANT_PAGE_SIZE = 25

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
    """Render the public introduction or an authenticated project overview."""
    recent_owned: list[object] = []
    recent_shared: list[object] = []
    is_authenticated = False
    if isinstance(request.user, User) and request.user.is_authenticated:
        is_authenticated = True
        owned = owned_projects(request.user.id)
        shared = shared_projects(request.user.id)
        recent_owned = list(owned[:5])
        recent_shared = list(shared[:5])
    return render(
        request,
        "portal/home.html",
        {
            "content_origin": settings.AGORA_CONTENT_ORIGIN,
            "environment": settings.AGORA_ENVIRONMENT,
            "hide_account_menu": not is_authenticated,
            "recent_owned": recent_owned,
            "recent_shared": recent_shared,
        },
    )


@login_required
@require_http_methods(["GET"])
def project_list(request: HttpRequest) -> HttpResponse:
    """Show either owner projects or currently available shared projects."""
    user = cast(User, request.user)
    scope = request.GET.get("scope")
    active_scope = "shared" if scope == "shared" else "mine"
    mine_paginator = Paginator(owned_projects(user.id), PROJECT_PAGE_SIZE)
    shared_paginator = Paginator(shared_projects(user.id), PROJECT_PAGE_SIZE)
    page_number = request.GET.get("page")
    project_page = (
        shared_paginator.get_page(page_number)
        if active_scope == "shared"
        else mine_paginator.get_page(page_number)
    )
    return render(
        request,
        "portal/projects/list.html",
        {
            "active_scope": active_scope,
            "projects": project_page,
            "project_page": project_page,
            "mine_count": mine_paginator.count,
            "shared_count": shared_paginator.count,
        },
    )


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
    revisions = (
        Paginator(
            project_revisions(project.id, user.id).order_by("-number", "-created_at", "-id"),
            REVISION_PAGE_SIZE,
        ).get_page(request.GET.get("page"))
        if is_owner
        else []
    )
    return render(
        request,
        "portal/projects/detail.html",
        {
            "project": project,
            "is_owner": is_owner,
            "revisions": revisions,
            "viewer_count": project_effective_viewer_count(project.id, user.id) if is_owner else 0,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_access(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """List and manage Viewer epochs for one owner-controlled project."""
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return render(request, "portal/not_found.html", status=404)

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

    active_grants = project_active_grants(project.id, user.id).order_by(
        "viewer__soeid", "created_at", "id"
    )
    grant_history = project_grant_history(project.id, user.id).order_by("-revoked_at", "-id")
    active_page = Paginator(active_grants, GRANT_PAGE_SIZE).get_page(request.GET.get("page"))
    history_page = Paginator(grant_history, GRANT_PAGE_SIZE).get_page(
        request.GET.get("history_page")
    )
    return render(
        request,
        "portal/projects/access.html",
        {
            "project": project,
            "owner_soeid": user.soeid,
            "form": form,
            "active_grants": active_page,
            "active_grants_page": active_page,
            "grant_history": history_page,
            "grant_history_page": history_page,
            "viewer_count": project_effective_viewer_count(project.id, user.id),
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

    grant = project_active_grants(project.id, user.id).filter(id=grant_id).first()
    if grant is None:
        # Retain a safe retry path after another request has already closed the epoch.
        grant = project_grant_history(project.id, user.id).filter(id=grant_id).first()
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
@require_http_methods(["GET"])
def project_view(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Resolve the stable project URL to the currently pinned published revision."""
    user = cast(User, request.user)
    resolved = visible_project(project_id, user.id)
    if resolved is None:
        return render(request, "portal/not_found.html", status=404)
    project, _ = resolved
    if project.published_revision is None:
        return render(request, "portal/not_found.html", status=404)
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
            "artifacts": list(revision.artifacts.all()),
            "is_preview": is_preview,
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
        html_file: UploadedFile[Any] = cast(Any, request.FILES["html_file"])
        csv_files: list[UploadedFile[Any]] = request.FILES.getlist("csv_files")
        parts = [_upload_part(html_file), *(_upload_part(item) for item in csv_files)]
        storage = FilesystemArtifactStorage(Path(settings.AGORA_ARTIFACT_ROOT))
        try:
            revision = create_upload_revision(
                dashboard_id=project.id,
                created_by_id=user.id,
                parts=parts,
                storage=storage,
            )
        except UploadRejected as error:
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
    if error.issue.part_index is None:
        return message
    location = "dashboard HTML" if error.issue.part_index == 0 else "CSV attachment"
    return f"Problem with the {location}: {message}"


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
    """List identity status without exposing passwords or internal identifiers."""
    users = User.objects.only("id", "soeid", "password", "is_active", "is_administrator").order_by(
        "soeid"
    )
    return render(request, "portal/admin/user_list.html", {"users": users})


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
