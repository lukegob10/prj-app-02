"""Trusted portal views for local authentication and user administration."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

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
from agora.persistence.models import User

from .forms import ConfirmActionForm, LoginForm, ProvisionUserForm, ResetPasswordForm
from .security import administrator_required, safe_next_url

GENERIC_LOGIN_ERROR = "Sign-in failed. Check your SOEID and password."


def home(request: HttpRequest) -> HttpResponse:
    """Render the trusted portal home and the available identity actions."""
    return render(
        request,
        "portal/home.html",
        {
            "content_origin": settings.AGORA_CONTENT_ORIGIN,
            "environment": settings.AGORA_ENVIRONMENT,
        },
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
