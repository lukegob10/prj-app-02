"""Shared context for the trusted portal shell."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse


def portal_shell(request: HttpRequest) -> dict[str, Any]:
    """Expose only real routes and the current trusted account to the shell."""
    user = getattr(request, "user", None)
    route_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    nav_items: list[dict[str, object]] = [
        {
            "label": "Home",
            "url": reverse("home"),
            "current": route_name == "home",
        }
    ]

    if getattr(user, "is_authenticated", False) and getattr(user, "is_administrator", False):
        nav_items.append(
            {
                "label": "Users",
                "url": reverse("admin-user-list"),
                "current": bool(route_name and route_name.startswith("admin-user-")),
            }
        )

    if getattr(user, "is_authenticated", False):
        account = {
            "name": str(getattr(user, "soeid", "")),
            "logout_url": reverse("logout"),
        }
    else:
        account = {"login_url": reverse("login")}

    return {
        "account": account,
        "environment": settings.AGORA_ENVIRONMENT,
        "nav_items": nav_items,
    }
