"""Shared context for the trusted portal shell."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse

from agora.portal.development import development_source_version


def portal_shell(request: HttpRequest) -> dict[str, Any]:
    """Expose only real routes and the current trusted account to the shell."""
    user = getattr(request, "user", None)
    route_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    is_authenticated = getattr(user, "is_authenticated", False)
    nav_items: list[dict[str, object]] = [
        {
            "label": "Projects" if is_authenticated else "Home",
            "url": reverse("home"),
            "current": bool(
                route_name == "home"
                or (is_authenticated and route_name and route_name.startswith("project-"))
            ),
        }
    ]

    if not is_authenticated and route_name != "login":
        nav_items.append(
            {
                "label": "Sign In",
                "url": reverse("login"),
                "current": False,
                "variant": "primary",
            }
        )

    if is_authenticated and getattr(user, "is_administrator", False):
        nav_items.append(
            {
                "label": "Users",
                "url": reverse("admin-user-list"),
                "current": bool(route_name and route_name.startswith("admin-user-")),
            }
        )

    if is_authenticated:
        account = {
            "name": str(getattr(user, "soeid", "")),
            "logout_url": reverse("logout"),
        }
    else:
        account = {"login_url": reverse("login")}

    development_reload = None
    if getattr(settings, "AGORA_DEVELOPMENT_LIVE_RELOAD", False):
        development_reload = {
            "request_method": request.method,
            "url": reverse("development-reload-version"),
            "version": development_source_version(settings.BASE_DIR),
        }

    return {
        "account": account,
        "development_reload": development_reload,
        "environment": settings.AGORA_ENVIRONMENT,
        "nav_items": nav_items,
    }
