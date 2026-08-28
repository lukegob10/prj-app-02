"""Authorization, query-shape, and bounded-work coverage for discovery reads."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from django.db import connection
from django.db.models import Model, QuerySet, Subquery
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import agora.persistence.discovery as discovery
from agora.persistence.discovery import (
    DISCOVERY_CURSOR_COLUMNS,
    DISCOVERY_CURSOR_NAMESPACE,
    DiscoveryScope,
    DiscoverySearch,
    InvalidDiscoveryQuery,
    authorized_published_dashboard,
    favorite_dashboards,
    normalize_discovery_query,
    recently_viewed_dashboards,
    search_dashboards,
)
from agora.persistence.models import (
    Artifact,
    AuditEvent,
    AuthorizedOpen,
    Dashboard,
    DashboardFavorite,
    DashboardTag,
    DashboardViewerState,
    Revision,
    User,
    ViewerGrant,
)
from agora.persistence.pagination import InvalidCursor, paginate_keyset

pytestmark = pytest.mark.django_db


def _user(soeid: str) -> User:
    # The shared Oracle schema is retained between runs, so test identities must
    # never rely on a destructive flush for uniqueness.
    return User.objects.create_user(f"{soeid}.{uuid4().hex[:8].upper()}")


def _dashboard(owner: User, name: str) -> Dashboard:
    return Dashboard.objects.create(
        owner=owner,
        name=name,
        description="A description must never become discovery input or output.",
    )


def _publish(dashboard: Dashboard, *, published_at: datetime | None = None) -> Dashboard:
    revision = Revision.objects.create(
        dashboard=dashboard,
        number=1,
        created_by=dashboard.owner,
    )
    timestamp = published_at or timezone.now()
    dashboard.latest_revision = revision
    dashboard.published_revision = revision
    dashboard.first_published_at = timestamp
    dashboard.publication_version = 1
    dashboard.last_published_at = timestamp
    dashboard.state = Dashboard.State.PUBLISHED
    dashboard.save()
    return dashboard


def _grant(
    dashboard: Dashboard,
    *,
    viewer: User,
    owner: User,
    revoked: bool = False,
) -> ViewerGrant:
    return ViewerGrant.objects.create(
        dashboard=dashboard,
        viewer=viewer,
        created_by=owner,
        revoked_at=timezone.now() if revoked else None,
        revoked_by=owner if revoked else None,
    )


def _tag(dashboard: Dashboard, label: str, *, slot: int = 1) -> DashboardTag:
    return DashboardTag.objects.create(
        dashboard=dashboard,
        label=label,
        key="ignored-by-model-save",
        slot=slot,
    )


def _candidate_queries(queryset: QuerySet[Model]) -> list[Any]:
    candidates: list[Any] = []
    pending = [queryset.query.where]
    while pending:
        node = pending.pop()
        pending.extend(vars(node).get("children", ()))
        rhs = vars(node).get("rhs")
        if isinstance(rhs, Subquery):
            candidates.append(rhs.query)
    return candidates


def _index_fields(model: type[Model]) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def test_discovery_sources_follow_the_locked_leading_indexes() -> None:
    assert ("owner", "state") in _index_fields(Dashboard)
    assert ("viewer", "revoked_at", "-created_at", "-id") in _index_fields(ViewerGrant)
    assert ("key", "dashboard") in _index_fields(DashboardTag)
    assert ("user", "-created_at", "-id") in _index_fields(DashboardFavorite)
    assert ("user", "-last_viewed_at", "-id") in _index_fields(DashboardViewerState)

    # The locked schema has no normalized name key. Keep the name branch a
    # literal prefix inside an indexed authorization scope; never imply that it
    # is a case-insensitive, globally indexed text search.
    assert not any(fields[0] == "name" for fields in _index_fields(Dashboard))
    assert discovery.__file__ is not None
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    assert "name__startswith" in source
    assert "name__istartswith" not in source
    assert "description__" not in source


def test_search_normalization_and_cursor_context_bind_scope_identity_and_query() -> None:
    normalized = DiscoverySearch.from_input(
        scope="shared",
        query=(
            "  \N{FULLWIDTH LATIN CAPITAL LETTER R}"
            "\N{FULLWIDTH LATIN SMALL LETTER I}"
            "\N{FULLWIDTH LATIN SMALL LETTER S}"
            "\N{FULLWIDTH LATIN SMALL LETTER K}"
            "\N{NO-BREAK SPACE}"
            "\N{FULLWIDTH LATIN CAPITAL LETTER R}"
            "\N{FULLWIDTH LATIN SMALL LETTER E}"
            "\N{FULLWIDTH LATIN SMALL LETTER P}"
            "\N{FULLWIDTH LATIN SMALL LETTER O}"
            "\N{FULLWIDTH LATIN SMALL LETTER R}"
            "\N{FULLWIDTH LATIN SMALL LETTER T}  "
        ),
    )
    assert normalized.scope is DiscoveryScope.SHARED
    assert normalized.query == "Risk Report"
    assert normalize_discovery_query("  Risk   Report ") == "Risk Report"

    principal = uuid4()
    context = normalized.cursor_context(principal_id=principal)
    assert str(principal) in context
    assert '"scope":"shared"' in context
    assert '"query":"Risk Report"' in context
    assert context != DiscoverySearch.from_input(
        scope="mine",
        query="Risk Report",
    ).cursor_context(principal_id=principal)
    assert context != DiscoverySearch.from_input(
        scope="shared",
        query="Risk",
    ).cursor_context(principal_id=principal)

    with pytest.raises(InvalidDiscoveryQuery, match="control"):
        normalize_discovery_query("Risk\nReport")
    with pytest.raises(InvalidDiscoveryQuery, match="at most 200"):
        normalize_discovery_query("x" * 201)
    with pytest.raises(ValueError, match="mine or shared"):
        DiscoverySearch.from_input(scope="everywhere")


def test_search_is_explicitly_scoped_authorized_prefix_only_and_duplicate_free() -> None:
    owner = _user("DISCOVERY.OWNER")
    other_owner = _user("DISCOVERY.OTHER")
    viewer = _user("DISCOVERY.VIEWER")
    outsider = _user("DISCOVERY.OUTSIDER")
    disabled_viewer = _user("DISCOVERY.DISABLED.VIEWER")
    disabled_owner = _user("DISCOVERY.DISABLED.OWNER")

    mine = _dashboard(viewer, "Risk owner draft")
    mine_archived = _dashboard(viewer, "Risk retained archive")
    Dashboard.objects.filter(id=mine_archived.id).update(state=Dashboard.State.ARCHIVED)
    shared = _publish(_dashboard(owner, "Risk published report"))
    revoked = _publish(_dashboard(owner, "Risk revoked report"))
    unrelated = _publish(_dashboard(other_owner, "Risk private publication"))
    disabled = _publish(_dashboard(disabled_owner, "Risk disabled owner"))
    _grant(shared, viewer=viewer, owner=owner)
    _grant(shared, viewer=disabled_viewer, owner=owner)
    _grant(revoked, viewer=viewer, owner=owner, revoked=True)
    _grant(disabled, viewer=viewer, owner=disabled_owner)
    disabled_viewer.is_active = False
    disabled_viewer.save(update_fields=("is_active",))
    disabled_owner.is_active = False
    disabled_owner.save(update_fields=("is_active",))

    # Multiple matching tags cannot duplicate the dashboard because matching uses EXISTS.
    _tag(shared, "Finance Risk", slot=1)
    _tag(shared, "Risk Controls", slot=2)

    assert {
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.MINE,
            query="Risk",
        )
    } == {mine.id, mine_archived.id}
    assert [
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
            query="Risk",
        )
    ] == [disabled.id, shared.id]
    assert [
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
            query="DISCOVERY.OWN",
        )
    ] == [shared.id]
    assert [
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
            query="finance r",
        )
    ] == [shared.id]
    assert [
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
            query="Finance Risk",
        )
    ] == [shared.id]

    for principal in (outsider, disabled_viewer):
        assert (
            list(
                search_dashboards(
                    principal_id=principal.id,
                    scope=DiscoveryScope.SHARED,
                    query="Risk",
                )
            )
            == []
        )
    assert (
        list(
            search_dashboards(
                principal_id=disabled_owner.id,
                scope=DiscoveryScope.MINE,
            )
        )
        == []
    )
    assert unrelated.id not in {
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
        )
    }
    assert revoked.id not in {
        row.id
        for row in search_dashboards(
            principal_id=viewer.id,
            scope=DiscoveryScope.SHARED,
        )
    }


def test_search_rows_are_safe_template_ready_and_do_not_fetch_artifacts_or_events() -> None:
    owner = _user("SHAPE.OWNER")
    viewer = _user("SHAPE.VIEWER")
    dashboard = _publish(_dashboard(owner, "Shape report"))
    _grant(dashboard, viewer=viewer, owner=owner)
    _tag(dashboard, "Shape")
    DashboardFavorite.objects.create(user=viewer, dashboard=dashboard)

    with CaptureQueriesContext(connection) as queries:
        rows = list(
            search_dashboards(
                principal_id=viewer.id,
                scope=DiscoveryScope.SHARED,
                query="Shape",
            )
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.owner.soeid == owner.soeid
        assert row.published_revision is not None
        assert row.published_revision.number == 1
        assert cast(Any, row).is_favorite is True

    assert len(queries) == 1
    assert "description" in row.get_deferred_fields()
    assert "latest_revision_id" in row.get_deferred_fields()
    sql = "\n".join(query["sql"] for query in queries).upper()
    assert "DESCRIPTION" not in sql
    assert "UPPER(" not in sql
    assert "LIKE" in sql
    assert Artifact._meta.db_table not in sql
    assert AuthorizedOpen._meta.db_table not in sql
    assert AuditEvent._meta.db_table not in sql
    assert "HTML" not in sql
    assert "CSV" not in sql


def test_search_uses_signed_25_plus_sentinel_keyset_without_count_or_offset() -> None:
    owner = _user("CURSOR.OWNER")
    dashboards = [_dashboard(owner, f"Cursor report {number:02d}") for number in range(27)]
    tied_at = timezone.now() - timedelta(days=1)
    Dashboard.objects.filter(id__in=[row.id for row in dashboards]).update(updated_at=tied_at)
    search = DiscoverySearch.from_input(scope="mine", query="Cursor report")
    queryset = search_dashboards(
        principal_id=owner.id,
        scope=search.scope,
        query=search.query,
    )

    with CaptureQueriesContext(connection) as first_queries:
        first = paginate_keyset(
            queryset,
            columns=DISCOVERY_CURSOR_COLUMNS,
            namespace=DISCOVERY_CURSOR_NAMESPACE,
            context=search.cursor_context(principal_id=owner.id),
        )
    assert len(first) == 25
    assert first.next_cursor is not None
    assert len(first_queries) == 1
    main_sql = first_queries[0]["sql"].upper()
    assert "COUNT(" not in main_sql
    assert " OFFSET " not in main_sql
    assert "DESCRIPTION" not in main_sql

    with CaptureQueriesContext(connection) as next_queries:
        second = paginate_keyset(
            queryset,
            columns=DISCOVERY_CURSOR_COLUMNS,
            namespace=DISCOVERY_CURSOR_NAMESPACE,
            context=search.cursor_context(principal_id=owner.id),
            cursor=first.next_cursor,
        )
    assert len(second) == 2
    assert len(next_queries) == len(first_queries)
    assert not ({row.id for row in first} & {row.id for row in second})

    for incompatible in (
        DiscoverySearch.from_input(scope="shared", query="Cursor report"),
        DiscoverySearch.from_input(scope="mine", query="Cursor"),
    ):
        with pytest.raises(InvalidCursor):
            paginate_keyset(
                queryset,
                columns=DISCOVERY_CURSOR_COLUMNS,
                namespace=DISCOVERY_CURSOR_NAMESPACE,
                context=incompatible.cursor_context(principal_id=owner.id),
                cursor=first.next_cursor,
            )
    assert first.next_cursor is not None
    tampered = f"{first.next_cursor[:-1]}{'A' if first.next_cursor[-1] != 'A' else 'B'}"
    with pytest.raises(InvalidCursor):
        paginate_keyset(
            queryset,
            columns=DISCOVERY_CURSOR_COLUMNS,
            namespace=DISCOVERY_CURSOR_NAMESPACE,
            context=search.cursor_context(principal_id=owner.id),
            cursor=tampered,
        )


def test_favorites_are_bounded_and_stale_rows_never_restore_access() -> None:
    owner = _user("FAVORITE.OWNER")
    viewer = _user("FAVORITE.VIEWER")
    disabled_viewer = _user("FAVORITE.DISABLED")
    disabled_owner = _user("FAVORITE.DISABLED.OWNER")
    visible = _publish(_dashboard(owner, "Visible favorite"))
    revoked = _publish(_dashboard(owner, "Revoked favorite"))
    unpublished = _publish(_dashboard(owner, "Unpublished favorite"))
    archived = _publish(_dashboard(owner, "Archived favorite"))
    deleted = _publish(_dashboard(owner, "Deleted favorite"))
    disabled_owner_dashboard = _publish(_dashboard(disabled_owner, "Disabled-owner favorite"))
    _grant(visible, viewer=viewer, owner=owner)
    _grant(revoked, viewer=viewer, owner=owner, revoked=True)
    _grant(
        disabled_owner_dashboard,
        viewer=viewer,
        owner=disabled_owner,
    )
    for row in (
        visible,
        revoked,
        unpublished,
        archived,
        deleted,
        disabled_owner_dashboard,
    ):
        DashboardFavorite.objects.create(user=viewer, dashboard=row)
    DashboardFavorite.objects.create(user=disabled_viewer, dashboard=visible)
    DashboardFavorite.objects.create(user=owner, dashboard=visible)
    DashboardFavorite.objects.create(user=owner, dashboard=unpublished)
    DashboardFavorite.objects.create(user=owner, dashboard=archived)
    DashboardFavorite.objects.create(user=owner, dashboard=deleted)
    Dashboard.objects.filter(id=unpublished.id).update(
        state=Dashboard.State.UNPUBLISHED,
        published_revision_id=None,
    )
    Dashboard.objects.filter(id=archived.id).update(
        state=Dashboard.State.ARCHIVED,
        published_revision_id=None,
    )
    Dashboard.objects.filter(id=deleted.id).update(
        state=Dashboard.State.DELETED,
        published_revision_id=None,
    )
    disabled_viewer.is_active = False
    disabled_viewer.save(update_fields=("is_active",))
    disabled_owner.is_active = False
    disabled_owner.save(update_fields=("is_active",))

    with CaptureQueriesContext(connection) as queries:
        rows = list(favorite_dashboards(user_id=viewer.id))
        assert [row.id for row in rows] == [disabled_owner_dashboard.id, visible.id]
        assert cast(Any, rows[0]).is_favorite is True
    assert len(queries) == 1
    assert list(favorite_dashboards(user_id=disabled_viewer.id)) == []
    assert DashboardFavorite.objects.filter(user=viewer).count() == 6
    assert [row.id for row in favorite_dashboards(user_id=owner.id)] == [visible.id]
    assert favorite_dashboards(user_id=viewer.id).query.high_mark == 10
    candidates = _candidate_queries(favorite_dashboards(user_id=viewer.id))
    assert any(candidate.high_mark == 100 for candidate in candidates)
    with pytest.raises(ValueError, match="between 1 and 10"):
        favorite_dashboards(user_id=viewer.id, limit=11)

    with CaptureQueriesContext(connection) as eligibility_queries:
        eligible = authorized_published_dashboard(
            dashboard_id=visible.id,
            principal_id=viewer.id,
        )
    assert eligible is not None
    assert len(eligibility_queries) == 1
    assert "name" in eligible.get_deferred_fields()
    for hidden in (
        revoked,
        unpublished,
        archived,
        deleted,
    ):
        assert (
            authorized_published_dashboard(
                dashboard_id=hidden.id,
                principal_id=viewer.id,
            )
            is None
        )
    assert (
        authorized_published_dashboard(
            dashboard_id=disabled_owner_dashboard.id,
            principal_id=viewer.id,
        )
        is not None
    )


def test_recently_viewed_reads_only_compact_state_and_is_bounded() -> None:
    owner = _user("RECENT.OWNER")
    disabled_owner = _user("RECENT.DISABLED.OWNER")
    viewer = _user("RECENT.VIEWER")
    visible = _publish(_dashboard(owner, "Recent visible"))
    revoked = _publish(_dashboard(owner, "Recent revoked"))
    disabled_owner_dashboard = _publish(_dashboard(disabled_owner, "Recent disabled owner"))
    _grant(visible, viewer=viewer, owner=owner)
    _grant(revoked, viewer=viewer, owner=owner, revoked=True)
    _grant(disabled_owner_dashboard, viewer=viewer, owner=disabled_owner)
    now = timezone.now()
    DashboardViewerState.objects.create(
        user=viewer,
        dashboard=visible,
        last_viewed_at=now,
        seen_publication_version=0,
    )
    DashboardViewerState.objects.create(
        user=viewer,
        dashboard=revoked,
        last_viewed_at=now + timedelta(seconds=1),
        seen_publication_version=1,
    )
    DashboardViewerState.objects.create(
        user=viewer,
        dashboard=disabled_owner_dashboard,
        last_viewed_at=now + timedelta(seconds=2),
        seen_publication_version=0,
    )
    disabled_owner.is_active = False
    disabled_owner.save(update_fields=("is_active",))

    with CaptureQueriesContext(connection) as queries:
        rows = list(recently_viewed_dashboards(user_id=viewer.id))
        assert [row.id for row in rows] == [disabled_owner_dashboard.id, visible.id]
        assert cast(Any, rows[0]).has_new_publication is True
        assert cast(Any, rows[0]).is_favorite is False
    assert len(queries) == 1
    sql = "\n".join(query["sql"] for query in queries).upper()
    assert DashboardViewerState._meta.db_table in sql
    assert AuthorizedOpen._meta.db_table not in sql
    assert AuditEvent._meta.db_table not in sql
    assert recently_viewed_dashboards(user_id=viewer.id).query.high_mark == 10
    candidates = _candidate_queries(recently_viewed_dashboards(user_id=viewer.id))
    assert any(candidate.high_mark == 100 for candidate in candidates)
    with pytest.raises(ValueError, match="between 1 and 10"):
        recently_viewed_dashboards(user_id=viewer.id, limit=0)


def test_query_module_has_no_artifact_or_raw_event_dependency() -> None:
    assert discovery.__file__ is not None
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "Artifact",
        "AuthorizedOpen",
        "AuditEvent",
        "authorized_open_events",
        "artifacts",
        "description__",
        "icontains",
        "contains=",
    ):
        assert forbidden not in source
