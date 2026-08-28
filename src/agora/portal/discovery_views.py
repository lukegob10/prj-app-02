"""Server-rendered dashboard discovery, tags, and personal shortcuts."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from agora.persistence.discovery import (
    DISCOVERY_CURSOR_COLUMNS,
    DISCOVERY_CURSOR_NAMESPACE,
    DISCOVERY_PAGE_SIZE,
    DiscoveryScope,
    DiscoverySearch,
    authorized_published_dashboard,
    favorite_dashboards,
    recently_viewed_dashboards,
    search_dashboards,
)
from agora.persistence.enhancement_queries import dashboard_tags
from agora.persistence.enhancements import (
    EnhancementAccessDenied,
    EnhancementValidationError,
    replace_dashboard_tags,
    set_dashboard_favorite,
)
from agora.persistence.models import Dashboard, User
from agora.persistence.pagination import CursorPage, InvalidCursor, paginate_keyset
from agora.persistence.projects import manageable_project, owned_projects, shared_projects

from .discovery_forms import DashboardSearchForm, DashboardTagsForm
from .security import safe_next_url

HOME_PROJECT_LIMIT = 5


def home(request: HttpRequest) -> HttpResponse:
    """Render the public introduction or bounded authenticated return surfaces."""

    recent_owned: tuple[Dashboard, ...] = ()
    recent_shared: tuple[Dashboard, ...] = ()
    favorites: tuple[Dashboard, ...] = ()
    recently_viewed: tuple[Dashboard, ...] = ()
    is_authenticated = isinstance(request.user, User) and request.user.is_authenticated
    if is_authenticated:
        user = cast(User, request.user)
        recent_owned = tuple(owned_projects(user.id)[:HOME_PROJECT_LIMIT])
        recent_shared = tuple(shared_projects(user.id)[:HOME_PROJECT_LIMIT])
        favorites = tuple(favorite_dashboards(user_id=user.id))
        recently_viewed = tuple(recently_viewed_dashboards(user_id=user.id))

    return render(
        request,
        "portal/home.html",
        {
            "content_origin": settings.AGORA_CONTENT_ORIGIN,
            "environment": settings.AGORA_ENVIRONMENT,
            "favorites": favorites,
            "hide_account_menu": not is_authenticated,
            "recent_owned": recent_owned,
            "recent_shared": recent_shared,
            "recently_viewed": recently_viewed,
        },
    )


@login_required
@require_http_methods(["GET"])
def project_list(request: HttpRequest, *, page_size: int = DISCOVERY_PAGE_SIZE) -> HttpResponse:
    """Search one explicit owner or active-viewer scope with signed keyset paging."""

    user = cast(User, request.user)
    scope = (
        DiscoveryScope.SHARED
        if request.GET.get("scope") == DiscoveryScope.SHARED.value
        else DiscoveryScope.MINE
    )
    search_form = DashboardSearchForm({"query": request.GET.get("query", "")})
    search_query = ""
    project_page: CursorPage[Dashboard]

    if search_form.is_valid():
        search_query = cast(str, search_form.cleaned_data["query"])
        search = DiscoverySearch.from_input(scope=scope, query=search_query)
        try:
            project_page = paginate_keyset(
                search_dashboards(
                    principal_id=user.id,
                    scope=search.scope,
                    query=search.query,
                ),
                columns=DISCOVERY_CURSOR_COLUMNS,
                namespace=DISCOVERY_CURSOR_NAMESPACE,
                context=search.cursor_context(principal_id=user.id),
                cursor=request.GET.get("cursor"),
                page_size=page_size,
            )
        except InvalidCursor:
            return render(request, "portal/not_found.html", status=404)
    else:
        project_page = CursorPage(items=(), previous_cursor=None, next_cursor=None)

    list_url = reverse("project-list")
    scope_query = {"scope": scope.value} if scope is DiscoveryScope.SHARED else {}
    preserved_query = {**scope_query, "query": search_query}
    project_page = project_page.with_urls(
        base_url=list_url,
        cursor_parameter="cursor",
        preserved_query=preserved_query,
    )
    shared_url = f"{list_url}?{urlencode({'scope': DiscoveryScope.SHARED.value})}"
    clear_search_url = shared_url if scope is DiscoveryScope.SHARED else list_url
    return render(
        request,
        "portal/projects/list.html",
        {
            "active_scope": scope.value,
            "clear_search_url": clear_search_url,
            "mine_url": list_url,
            "project_page": project_page,
            "projects": project_page.items,
            "search_active": bool(search_query),
            "search_form": search_form,
            "search_input": search_query if not search_form.errors else "",
            "search_query": search_query,
            "search_url": list_url,
            "shared_url": shared_url,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def project_tags(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Replace an active owner's bounded tag set on a compact separate page."""

    user = cast(User, request.user)
    project = manageable_project(project_id, user.id)
    if project is None or project.state in {Dashboard.State.ARCHIVED, Dashboard.State.DELETED}:
        return render(request, "portal/not_found.html", status=404)

    initial = {
        f"tag_{position}": tag.label
        for position, tag in enumerate(
            dashboard_tags(dashboard_id=project.id, principal_id=user.id, limit=5),
            start=1,
        )
    }
    tag_form = DashboardTagsForm(
        request.POST if request.method == "POST" else None,
        initial=initial,
    )
    if request.method == "POST" and tag_form.is_valid():
        try:
            replace_dashboard_tags(
                dashboard_id=project.id,
                actor_id=user.id,
                labels=tag_form.labels,
            )
        except EnhancementValidationError as error:
            tag_form.add_error(None, str(error))
        except EnhancementAccessDenied:
            return render(request, "portal/not_found.html", status=404)
        else:
            messages.success(request, "Tags saved.")
            return redirect("project-tags", project_id=project.id)

    return render(
        request,
        "portal/discovery/tags.html",
        {
            "cancel_url": reverse("project-list"),
            "project": project,
            "tag_action_url": reverse("project-tags", args=[project.id]),
            "tag_form": tag_form,
        },
    )


@login_required
@require_POST
def project_favorite(request: HttpRequest, project_id: UUID) -> HttpResponse:
    """Idempotently toggle one currently authorized Published dashboard shortcut."""

    user = cast(User, request.user)
    requested_state = request.POST.get("favorited")
    if requested_state not in {"0", "1"}:
        return render(request, "portal/not_found.html", status=404)
    if authorized_published_dashboard(dashboard_id=project_id, principal_id=user.id) is None:
        return render(request, "portal/not_found.html", status=404)

    favorited = requested_state == "1"
    try:
        set_dashboard_favorite(
            dashboard_id=project_id,
            user_id=user.id,
            favorited=favorited,
        )
    except EnhancementAccessDenied:
        return render(request, "portal/not_found.html", status=404)

    messages.success(
        request,
        "Added to favorites." if favorited else "Removed from favorites.",
    )
    return redirect(safe_next_url(request, request.POST.get("next")))


__all__ = ["home", "project_favorite", "project_list", "project_tags"]
