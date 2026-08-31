"""Owner-only request queue and exact-epoch ownership transfer views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from agora.core.enhancements import (
    EnhancementAccessDenied,
    resolve_dashboard_access_request,
    transfer_dashboard_ownership,
)
from agora.core.models import AccessRequest, Dashboard, User
from agora.core.pagination import (
    CursorColumn,
    CursorValueKind,
    InvalidCursor,
    paginate_keyset,
)
from agora.core.projects import manageable_project
from agora.core.stewardship_queries import owner_pending_access_requests

from .stewardship_forms import TransferOwnershipConfirmForm, TransferOwnershipForm

REQUEST_CURSOR_COLUMNS = (
    CursorColumn("requested_at", CursorValueKind.DATETIME, descending=True),
    CursorColumn("id", CursorValueKind.INTEGER, descending=True),
)
REQUEST_PAGE_SIZE = 25
_TRANSFER_CONFIRMATION_VERSION: Final = 1
_TRANSFER_CONFIRMATION_MAX_AGE_SECONDS: Final = 900
_TRANSFER_CONFIRMATION_MAX_LENGTH: Final = 2_048
_TRANSFER_CONFIRMATION_SALT: Final = "agora.stewardship.transfer-confirmation.v1"
_TRANSFERABLE_STATES: Final = frozenset(
    {
        Dashboard.State.DRAFT,
        Dashboard.State.PUBLISHED,
        Dashboard.State.UNPUBLISHED,
    }
)


@dataclass(frozen=True, slots=True)
class _TransferTarget:
    user: User
    token: str
    expected_transfer_id: UUID | None


@login_required
@require_http_methods(["GET"])
def project_access_requests(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Show one current owner's bounded, pending-only dashboard request queue."""

    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return _not_found(request)

    try:
        request_page = paginate_keyset(
            owner_pending_access_requests(dashboard_id=project.id, owner_id=user.id),
            columns=REQUEST_CURSOR_COLUMNS,
            namespace="project-access-requests",
            context=f"{project.id}:{user.id}",
            cursor=request.GET.get("request_cursor"),
            page_size=REQUEST_PAGE_SIZE,
        )
    except InvalidCursor:
        return _not_found(request)

    request_page = request_page.with_urls(
        base_url=reverse("project-access-requests", args=[project.id]),
        cursor_parameter="request_cursor",
    )
    return render(
        request,
        "portal/projects/access_requests.html",
        {
            "project": project,
            "access_requests": request_page.items,
            "request_page": request_page,
            "access_url": reverse("project-access", args=[project.id]),
        },
    )


@login_required
@require_POST
def project_access_request_approve(
    request: HttpRequest,
    project_id: UUID,
    request_id: int,
) -> HttpResponse:
    """Atomically approve one pending request and preserve/create its Viewer grant."""

    return _resolve_access_request(
        request,
        project_id=project_id,
        request_id=request_id,
        resolution=AccessRequest.Status.APPROVED,
    )


@login_required
@require_POST
def project_access_request_decline(
    request: HttpRequest,
    project_id: UUID,
    request_id: int,
) -> HttpResponse:
    """Atomically deny one pending request without creating authority."""

    return _resolve_access_request(
        request,
        project_id=project_id,
        request_id=request_id,
        resolution=AccessRequest.Status.DENIED,
    )


def _resolve_access_request(
    request: HttpRequest,
    *,
    project_id: UUID,
    request_id: int,
    resolution: AccessRequest.Status,
) -> HttpResponse:
    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None:
        return _not_found(request)

    try:
        access_request = resolve_dashboard_access_request(
            dashboard_id=project.id,
            request_id=request_id,
            actor_id=user.id,
            resolution=resolution,
        )
    except EnhancementAccessDenied:
        messages.warning(
            request,
            "That request changed or is no longer eligible. The queue has been refreshed.",
        )
    else:
        if resolution == AccessRequest.Status.APPROVED:
            messages.success(
                request,
                f"Viewer access is approved for {access_request.requester.soeid}.",
            )
        else:
            messages.success(
                request,
                f"The request from {access_request.requester.soeid} was denied.",
            )
    return redirect("project-access-requests", project_id=project.id)


@login_required
@require_http_methods(["GET", "POST"])
def project_transfer(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Identify an active incoming owner before a separate signed confirmation."""

    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None or project.state not in _TRANSFERABLE_STATES:
        return _not_found(request)

    form = TransferOwnershipForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        incoming_soeid = cast(str, form.cleaned_data["incoming_owner_soeid"])
        incoming_owner = (
            User.objects.filter(soeid=incoming_soeid, is_active=True)
            .only("id", "soeid", "is_active")
            .first()
        )
        if incoming_owner is None:
            form.add_error(
                "incoming_owner_soeid",
                "No active user was found for that SOEID.",
            )
        elif incoming_owner.id == user.id:
            form.add_error(
                "incoming_owner_soeid",
                "Choose another active user; you already have Full control.",
            )
        else:
            token = _sign_transfer_confirmation(
                project=project,
                actor=user,
                incoming_owner=incoming_owner,
            )
            confirm_url = reverse("project-transfer-confirm", args=[project.id])
            return redirect(f"{confirm_url}?{urlencode({'confirmation': token})}")

    if form.errors:
        form.fields["incoming_owner_soeid"].widget.attrs["aria-describedby"] = (
            "incoming-owner-soeid-help incoming-owner-soeid-error"
        )
        form.fields["incoming_owner_soeid"].widget.attrs["aria-invalid"] = "true"

    return render(
        request,
        "portal/projects/transfer_ownership.html",
        {
            "project": project,
            "form": form,
            "transfer_url": reverse("project-transfer", args=[project.id]),
            "access_url": reverse("project-access", args=[project.id]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_transfer_confirm(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Confirm a target bound to the caller's exact current ownership epoch."""

    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None or project.state not in _TRANSFERABLE_STATES:
        return _not_found(request)

    token = (
        request.POST.get("confirmation_token")
        if request.method == "POST"
        else request.GET.get("confirmation")
    )
    target = _load_transfer_target(token, project=project, actor=user)
    if target is None:
        messages.warning(
            request,
            "That ownership confirmation is invalid or no longer current. Start again.",
        )
        return redirect("project-transfer", project_id=project.id)

    form = TransferOwnershipConfirmForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            transfer_dashboard_ownership(
                dashboard_id=project.id,
                actor_id=user.id,
                incoming_owner_id=target.user.id,
                expected_transfer_id=target.expected_transfer_id,
            )
        except EnhancementAccessDenied:
            messages.warning(
                request,
                "Ownership or account status changed before confirmation. Review and start again.",
            )
            if manageable_project(project.id, user.id) is None:
                return redirect("project-list")
            return redirect("project-transfer", project_id=project.id)
        messages.success(
            request,
            f"Ownership was transferred to {target.user.soeid}.",
        )
        return redirect("project-list")

    if form.errors:
        form.fields["confirm"].widget.attrs["aria-describedby"] = "transfer-confirm-error"
        form.fields["confirm"].widget.attrs["aria-invalid"] = "true"

    return render(
        request,
        "portal/projects/transfer_ownership_confirm.html",
        {
            "project": project,
            "incoming_owner_soeid": target.user.soeid,
            "form": form,
            "confirmation_token": target.token,
            "confirm_url": reverse("project-transfer-confirm", args=[project.id]),
            "access_url": reverse("project-access", args=[project.id]),
        },
    )


def _sign_transfer_confirmation(
    *,
    project: Dashboard,
    actor: User,
    incoming_owner: User,
) -> str:
    return signing.dumps(
        {
            "v": _TRANSFER_CONFIRMATION_VERSION,
            "project_id": str(project.id),
            "actor_id": str(actor.id),
            "incoming_owner_id": str(incoming_owner.id),
            "ownership_epoch": _ownership_epoch(project),
        },
        salt=_TRANSFER_CONFIRMATION_SALT,
    )


def _load_transfer_target(
    token: str | None,
    *,
    project: Dashboard,
    actor: User,
) -> _TransferTarget | None:
    if not isinstance(token, str) or not 1 <= len(token) <= _TRANSFER_CONFIRMATION_MAX_LENGTH:
        return None
    try:
        payload: object = signing.loads(
            token,
            salt=_TRANSFER_CONFIRMATION_SALT,
            max_age=_TRANSFER_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "project_id",
        "actor_id",
        "incoming_owner_id",
        "ownership_epoch",
    }:
        return None
    if (
        payload["v"] != _TRANSFER_CONFIRMATION_VERSION
        or payload["project_id"] != str(project.id)
        or payload["actor_id"] != str(actor.id)
    ):
        return None
    try:
        incoming_owner_id = UUID(payload["incoming_owner_id"])
    except AttributeError, TypeError, ValueError:
        return None
    epoch_value = payload["ownership_epoch"]
    if epoch_value == "":
        expected_transfer_id = None
    else:
        try:
            expected_transfer_id = UUID(epoch_value)
        except AttributeError, TypeError, ValueError:
            return None
    if project.last_ownership_transfer_id != expected_transfer_id:
        return None
    if incoming_owner_id == actor.id:
        return None
    incoming_owner = (
        User.objects.filter(id=incoming_owner_id, is_active=True)
        .only("id", "soeid", "is_active")
        .first()
    )
    if incoming_owner is None:
        return None
    return _TransferTarget(
        user=incoming_owner,
        token=token,
        expected_transfer_id=expected_transfer_id,
    )


def _ownership_epoch(project: Dashboard) -> str:
    return str(project.last_ownership_transfer_id or "")


def _not_found(request: HttpRequest) -> HttpResponse:
    return render(request, "portal/not_found.html", status=404)


__all__ = [
    "project_access_request_approve",
    "project_access_request_decline",
    "project_access_requests",
    "project_transfer",
    "project_transfer_confirm",
]
