"""Portal-only request guards and redirect validation."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request: HttpRequest, candidate: str | None) -> str:
    """Accept only a relative URL on the current portal origin."""
    fallback = reverse("home")
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    if any(character in candidate for character in ("\\", "\r", "\n")):
        return fallback
    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return fallback
    return candidate


def administrator_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Require a live application administrator; never trust a browser identity field."""

    @wraps(view)
    def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            next_url = safe_next_url(request, request.path)
            return redirect_to_login(next_url, settings.LOGIN_URL, REDIRECT_FIELD_NAME)
        if not getattr(user, "is_active", False) or not getattr(user, "is_administrator", False):
            return render(request, "portal/forbidden.html", status=403)
        return view(request, *args, **kwargs)

    return guarded
