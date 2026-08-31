"""Integrated discovery route and mutation coverage."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from agora.persistence.enhancements import EnhancementAccessDenied, set_dashboard_favorite
from agora.persistence.models import (
    Artifact,
    Dashboard,
    DashboardFavorite,
    DashboardTag,
    Revision,
    User,
    ViewerGrant,
)

pytestmark = pytest.mark.django_db


def _user(soeid: str) -> User:
    return User.objects.create_user(soeid)


def _dashboard(
    owner: User, name: str, *, description: str = "Hidden description term"
) -> Dashboard:
    return Dashboard.objects.create(owner=owner, name=name, description=description)


def _publish(dashboard: Dashboard) -> Dashboard:
    revision = Revision.objects.create(
        dashboard=dashboard,
        number=1,
        created_by=dashboard.owner,
    )
    dashboard.latest_revision = revision
    dashboard.published_revision = revision
    dashboard.first_published_at = timezone.now()
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.save(
        update_fields=(
            "latest_revision",
            "published_revision",
            "first_published_at",
            "state",
            "updated_at",
        )
    )
    return dashboard


def _grant(dashboard: Dashboard, viewer: User) -> ViewerGrant:
    return ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=viewer,
        created_by=dashboard.owner,
    )


def _client(user: User, *, enforce_csrf: bool = False) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf)
    client.force_login(user)
    return client


def test_projects_search_keeps_scope_and_query_inside_the_signed_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.PROJECT_PAGE_SIZE", 1)
    owner = _user("DISCOVERY.UI.OWNER")
    viewer = _user("DISCOVERY.UI.VIEWER")
    first = _publish(_dashboard(owner, "Treasury Alpha"))
    second = _publish(_dashboard(owner, "Treasury Beta"))
    for dashboard in (first, second):
        _grant(dashboard, viewer)
        DashboardTag.objects.create(
            dashboard=dashboard,
            label="Daily Risk",
            key="replaced-by-normalizer",
            slot=1,
        )

    client = _client(viewer)
    response = client.get(
        reverse("project-list"),
        {"scope": "shared", "query": "daily"},
    )

    assert response.status_code == 200
    assert response.context["active_scope"] == "shared"
    assert response.context["search_query"] == "daily"
    assert len(response.context["projects"]) == 1
    next_url = response.context["project_page"].next_url
    assert next_url is not None
    assert "scope=shared" in next_url and "query=daily" in next_url
    assert b"Hidden description term" not in response.content
    assert Artifact._meta.db_table.encode() not in response.content

    cursor = response.context["project_page"].next_cursor
    assert cursor is not None
    assert (
        client.get(
            reverse("project-list"),
            {"scope": "shared", "query": "Treasury", "cursor": cursor},
        ).status_code
        == 404
    )
    assert (
        client.get(
            reverse("project-list"),
            {"query": "daily", "cursor": cursor},
        ).status_code
        == 404
    )
    other = _client(_user("DISCOVERY.UI.OTHER"))
    assert other.get(next_url).status_code == 404


def test_invalid_search_is_inline_bounded_and_never_queries_dashboard_catalog() -> None:
    user = _user("DISCOVERY.INVALID")
    client = _client(user)
    oversized = "x" * 10_000

    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("project-list"), {"query": oversized})

    assert response.status_code == 200
    assert response.context["search_form"].errors
    assert response.context["projects"] == ()
    assert oversized.encode() not in response.content
    sql = "\n".join(query["sql"] for query in queries).upper()
    assert Dashboard._meta.db_table not in sql


def test_owner_tag_management_is_csrf_protected_normalized_and_generic_for_others() -> None:
    owner = _user("DISCOVERY.TAG.OWNER")
    outsider = _user("DISCOVERY.TAG.OUTSIDER")
    dashboard = _dashboard(owner, "Tag management secret")
    DashboardTag.objects.create(
        dashboard=dashboard,
        label="Existing",
        key="replaced-by-normalizer",
        slot=1,
    )
    url = reverse("project-tags", args=[dashboard.id])
    client = _client(owner, enforce_csrf=True)
    loaded = client.get(url)
    csrf = cast(str, client.cookies[settings.CSRF_COOKIE_NAME].value)

    assert loaded.status_code == 200
    assert b'value="Existing"' in loaded.content
    assert client.post(url, {"tag_1": "Finance"}).status_code == 403

    duplicate = client.post(
        url,
        {
            "tag_1": "  Daily   Risk ",
            "tag_2": "DAILY RISK",
            "tag_3": "",
            "tag_4": "",
            "tag_5": "",
        },
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert duplicate.status_code == 200
    assert b"matches another tag after normalization" in duplicate.content
    assert list(
        DashboardTag.objects.filter(dashboard=dashboard).values_list("label", flat=True)
    ) == ["Existing"]

    saved = client.post(
        url,
        {
            "tag_1": "  Daily   Risk ",
            "tag_2": "Treasury",
            "tag_3": "",
            "tag_4": "",
            "tag_5": "",
        },
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert saved.status_code == 302
    assert saved["Location"] == url
    assert list(
        DashboardTag.objects.filter(dashboard=dashboard)
        .order_by("slot")
        .values_list("label", flat=True)
    ) == ["Daily Risk", "Treasury"]

    cleared = client.post(
        url,
        {field_name: "" for field_name in ("tag_1", "tag_2", "tag_3", "tag_4", "tag_5")},
        HTTP_X_CSRFTOKEN=csrf,
    )
    assert cleared.status_code == 302
    assert DashboardTag.objects.filter(dashboard=dashboard).exists() is False

    hidden = _client(outsider).get(url)
    missing = _client(outsider).get(reverse("project-tags", args=[uuid4()]))
    assert hidden.status_code == missing.status_code == 404
    assert dashboard.name.encode() not in hidden.content


def test_favorite_post_is_idempotent_authorized_and_rechecks_published_state() -> None:
    owner = _user("DISCOVERY.FAVORITE.OWNER")
    viewer = _user("DISCOVERY.FAVORITE.VIEWER")
    outsider = _user("DISCOVERY.FAVORITE.OUTSIDER")
    dashboard = _publish(_dashboard(owner, "Favorite authorization secret"))
    grant = _grant(dashboard, viewer)
    url = reverse("project-favorite", args=[dashboard.id])
    client = _client(viewer)

    assert client.get(url).status_code == 405
    assert _client(viewer, enforce_csrf=True).post(url, {"favorited": "1"}).status_code == 403
    for _ in range(2):
        added = client.post(url, {"favorited": "1", "next": "/?scope=shared"})
        assert added.status_code == 302
        assert added["Location"] == "/?scope=shared"
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).count() == 1

    for _ in range(2):
        assert client.post(url, {"favorited": "0"}).status_code == 302
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).exists() is False
    readded = client.post(url, {"favorited": "1", "next": "https://attacker.example"})
    assert readded.status_code == 302
    assert readded["Location"] == reverse("home")

    owner.is_active = False
    owner.save(update_fields=("is_active",))
    assert client.post(url, {"favorited": "0"}).status_code == 302
    assert client.post(url, {"favorited": "1"}).status_code == 302
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).exists()

    hidden = _client(outsider).post(url, {"favorited": "1"})
    missing = _client(outsider).post(
        reverse("project-favorite", args=[uuid4()]),
        {"favorited": "1"},
    )
    assert hidden.status_code == missing.status_code == 404
    assert dashboard.name.encode() not in hidden.content

    owner.is_active = True
    owner.save(update_fields=("is_active",))
    grant.revoked_at = timezone.now()
    grant.revoked_by = owner
    grant.save(update_fields=("revoked_at", "revoked_by"))
    assert client.post(url, {"favorited": "0"}).status_code == 404
    assert DashboardFavorite.objects.filter(user=viewer, dashboard=dashboard).exists()
    assert dashboard.name.encode() not in client.get(reverse("home")).content

    Dashboard.objects.filter(id=dashboard.id).update(
        state=Dashboard.State.UNPUBLISHED,
        published_revision_id=None,
    )
    with pytest.raises(EnhancementAccessDenied):
        set_dashboard_favorite(
            dashboard_id=dashboard.id,
            user_id=owner.id,
            favorited=True,
        )


def test_legacy_projects_url_redirects_to_root_and_preserves_filters() -> None:
    user = _user("DISCOVERY.LEGACY")
    response = _client(user).get(
        reverse("legacy-project-list"),
        {"scope": "shared", "query": "risk"},
    )

    assert response.status_code == 302
    assert response["Location"] == "/?scope=shared&query=risk"
    assert reverse("project-list") == reverse("home") == "/"


def test_anonymous_discovery_mutations_and_lists_require_authentication() -> None:
    project_id = uuid4()
    client = Client()
    for method, url in (
        ("get", reverse("legacy-project-list")),
        ("get", reverse("project-tags", args=[project_id])),
        ("post", reverse("project-favorite", args=[project_id])),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 302
        assert response["Location"].startswith(f"{reverse('login')}?next=")
