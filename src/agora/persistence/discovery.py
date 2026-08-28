"""Authorized, bounded query primitives for dashboard discovery.

The locked enhancement schema deliberately has no free-text index.  Discovery
therefore starts from an exact owner or active-grant scope and supports only
prefix predicates for dashboard names and canonical keys.  It never searches a
description, reads an artifact, or consults raw usage/audit history.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from django.db.models import (
    BooleanField,
    Case,
    Exists,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)

from agora.persistence.enhancement_queries import MAX_AUTHORIZATION_CANDIDATES
from agora.persistence.models import (
    Dashboard,
    DashboardFavorite,
    DashboardTag,
    DashboardViewerState,
    ViewerGrant,
)
from agora.persistence.names import (
    InvalidDashboardTag,
    InvalidSoeid,
    canonicalize_soeid,
    normalize_dashboard_tag,
)
from agora.persistence.pagination import CursorColumn, CursorValueKind

DISCOVERY_PAGE_SIZE: Final = 25
PERSONAL_DASHBOARD_LIMIT: Final = 10
MAX_DISCOVERY_QUERY_LENGTH: Final = 200
DISCOVERY_CURSOR_NAMESPACE: Final = "dashboard-discovery"
DISCOVERY_CURSOR_COLUMNS: Final = (
    CursorColumn("updated_at", CursorValueKind.DATETIME, descending=True),
    CursorColumn("id", CursorValueKind.UUID),
)


class DiscoveryScope(StrEnum):
    """The explicit authorization boundary for one Projects search."""

    MINE = "mine"
    SHARED = "shared"


class InvalidDiscoveryQuery(ValueError):
    """Raised when a discovery term is unsafe or cannot be bounded."""


@dataclass(frozen=True, slots=True)
class DiscoverySearch:
    """Normalized server-controlled state shared by querying and cursor signing."""

    scope: DiscoveryScope
    query: str

    @classmethod
    def from_input(
        cls,
        *,
        scope: DiscoveryScope | str,
        query: str = "",
    ) -> DiscoverySearch:
        try:
            resolved_scope = DiscoveryScope(scope)
        except ValueError as error:
            raise ValueError("discovery scope must be mine or shared") from error
        return cls(scope=resolved_scope, query=normalize_discovery_query(query))

    def cursor_context(self, *, principal_id: UUID) -> str:
        """Return an unambiguous identity/scope/filter context for signed cursors."""

        return json.dumps(
            {
                "principal_id": str(principal_id),
                "query": self.query,
                "scope": self.scope.value,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


def normalize_discovery_query(value: str) -> str:
    """Normalize one plain-text prefix without turning it into SQL or a tag."""

    if not isinstance(value, str):
        raise InvalidDiscoveryQuery("search must be text")
    compatible = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in compatible):
        raise InvalidDiscoveryQuery("search cannot contain control characters")
    normalized = " ".join(compatible.split())
    if len(normalized) > MAX_DISCOVERY_QUERY_LENGTH:
        raise InvalidDiscoveryQuery(
            f"search must contain at most {MAX_DISCOVERY_QUERY_LENGTH} characters"
        )
    return normalized


def search_dashboards(
    *,
    principal_id: UUID,
    scope: DiscoveryScope | str,
    query: str = "",
) -> QuerySet[Dashboard]:
    """Return a lazy, cursor-ready Dashboard search in one explicit access scope.

    ``mine`` preserves the existing retained owner list semantics: every state
    except Deleted remains visible to its active current owner.  ``shared`` is
    stricter and returns only Published dashboards with an active grant, active
    principal, and non-null published revision. A disabled owner does not revoke
    an otherwise active viewer grant.

    Matching is deliberately prefix-only.  Dashboard names use a literal
    ``LIKE prefix%`` predicate so Oracle is not forced through an unindexed
    case-conversion function.  SOEIDs and tags use their stored canonical keys.
    """

    search = DiscoverySearch.from_input(scope=scope, query=query)
    queryset = _scope_dashboards(principal_id=principal_id, scope=search.scope)
    if search.query:
        matches = Q(name__startswith=search.query)
        soeid_prefix = _soeid_prefix(search.query)
        if soeid_prefix is not None:
            matches |= Q(owner__soeid__startswith=soeid_prefix)
        tag_key_prefix = _tag_key_prefix(search.query)
        if tag_key_prefix is not None:
            matching_tag = DashboardTag.objects.filter(
                dashboard_id=OuterRef("pk"),
                key__startswith=tag_key_prefix,
            )
            matches |= Q(Exists(matching_tag))
        queryset = queryset.filter(matches)
    return _dashboard_rows(
        queryset,
        principal_id=principal_id,
        include_latest_revision=search.scope is DiscoveryScope.MINE,
    ).order_by(
        "-updated_at",
        "id",
    )


def favorite_dashboards(
    *,
    user_id: UUID,
    limit: int = PERSONAL_DASHBOARD_LIMIT,
) -> QuerySet[Dashboard]:
    """Return at most ten recent Favorites intersected with current access.

    The first stage reads no more than 100 rows from the purpose-built recent
    Favorite index.  The outer query drops stale rows after revoke, viewer
    disablement, unpublish, archive, or delete without deleting the retained
    preference.
    """

    bounded = _personal_limit(limit)
    candidates = (
        DashboardFavorite.objects.filter(user_id=user_id, user__is_active=True)
        .order_by("-created_at", "-id")
        .values("dashboard_id")[:MAX_AUTHORIZATION_CANDIDATES]
    )
    favorites = Dashboard.objects.filter(
        id__in=Subquery(candidates),
        favorites__user_id=user_id,
        favorites__user__is_active=True,
        state=Dashboard.State.PUBLISHED,
        published_revision__isnull=False,
    ).filter(_published_authorization(principal_id=user_id))
    return _dashboard_rows(favorites, principal_id=user_id).order_by(
        "-favorites__created_at",
        "-favorites__id",
    )[:bounded]


def recently_viewed_dashboards(
    *,
    user_id: UUID,
    limit: int = PERSONAL_DASHBOARD_LIMIT,
) -> QuerySet[Dashboard]:
    """Return at most ten recent authorized dashboards from compact viewer state."""

    bounded = _personal_limit(limit)
    candidates = (
        DashboardViewerState.objects.filter(user_id=user_id, user__is_active=True)
        .order_by("-last_viewed_at", "-id")
        .values("dashboard_id")[:MAX_AUTHORIZATION_CANDIDATES]
    )
    recent = (
        Dashboard.objects.filter(
            id__in=Subquery(candidates),
            viewer_states__user_id=user_id,
            viewer_states__user__is_active=True,
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
        )
        .filter(_published_authorization(principal_id=user_id))
        .annotate(
            has_new_publication=Case(
                When(
                    publication_version__gt=F("viewer_states__seen_publication_version"),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
    )
    return _dashboard_rows(recent, principal_id=user_id).order_by(
        "-viewer_states__last_viewed_at",
        "-viewer_states__id",
    )[:bounded]


def authorized_published_dashboard(
    *,
    dashboard_id: UUID,
    principal_id: UUID,
) -> Dashboard | None:
    """Resolve one active Published dashboard without disclosing hidden metadata.

    This is the read-side eligibility check for a Favorite POST.  The mutation
    remains delegated to :func:`agora.persistence.enhancements.set_dashboard_favorite`.
    """

    return (
        Dashboard.objects.filter(
            id=dashboard_id,
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
        )
        .filter(_published_authorization(principal_id=principal_id))
        .only("id")
        .first()
    )


def _scope_dashboards(
    *,
    principal_id: UUID,
    scope: DiscoveryScope,
) -> QuerySet[Dashboard]:
    if scope is DiscoveryScope.MINE:
        return Dashboard.objects.filter(
            owner_id=principal_id,
            owner__is_active=True,
        ).exclude(state=Dashboard.State.DELETED)

    active_dashboard_ids = ViewerGrant.objects.filter(
        viewer_id=principal_id,
        viewer__is_active=True,
        revoked_at__isnull=True,
    ).values("dashboard_id")
    return Dashboard.objects.filter(
        id__in=Subquery(active_dashboard_ids),
        state=Dashboard.State.PUBLISHED,
        published_revision__isnull=False,
    )


def _published_authorization(*, principal_id: UUID) -> Q:
    active_grant = ViewerGrant.objects.filter(
        dashboard_id=OuterRef("pk"),
        viewer_id=principal_id,
        viewer__is_active=True,
        revoked_at__isnull=True,
    )
    return Q(owner_id=principal_id, owner__is_active=True) | Q(Exists(active_grant))


def _dashboard_rows(
    queryset: QuerySet[Dashboard],
    *,
    principal_id: UUID,
    include_latest_revision: bool = False,
) -> QuerySet[Dashboard]:
    favorite = DashboardFavorite.objects.filter(
        dashboard_id=OuterRef("pk"),
        user_id=principal_id,
        user__is_active=True,
    )
    related: tuple[str, ...] = ("owner", "published_revision")
    fields: tuple[str, ...] = (
        "id",
        "owner",
        "name",
        "state",
        "published_revision",
        "publication_version",
        "updated_at",
        "owner__id",
        "owner__soeid",
        "published_revision__id",
        "published_revision__number",
    )
    if include_latest_revision:
        related += ("latest_revision",)
        fields += (
            "latest_revision",
            "latest_revision__id",
            "latest_revision__number",
        )
    return (
        queryset.annotate(
            is_favorite=Case(
                When(
                    state=Dashboard.State.PUBLISHED,
                    published_revision__isnull=False,
                    then=Exists(favorite),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
        .select_related(*related)
        .only(*fields)
    )


def _soeid_prefix(query: str) -> str | None:
    try:
        return canonicalize_soeid(query)
    except InvalidSoeid:
        return None


def _tag_key_prefix(query: str) -> str | None:
    try:
        return normalize_dashboard_tag(query).key
    except InvalidDashboardTag:
        return None


def _personal_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= PERSONAL_DASHBOARD_LIMIT:
        raise ValueError(f"limit must be between 1 and {PERSONAL_DASHBOARD_LIMIT}")
    return limit


__all__ = [
    "DISCOVERY_CURSOR_COLUMNS",
    "DISCOVERY_CURSOR_NAMESPACE",
    "DISCOVERY_PAGE_SIZE",
    "MAX_DISCOVERY_QUERY_LENGTH",
    "PERSONAL_DASHBOARD_LIMIT",
    "DiscoveryScope",
    "DiscoverySearch",
    "InvalidDiscoveryQuery",
    "authorized_published_dashboard",
    "favorite_dashboards",
    "normalize_discovery_query",
    "recently_viewed_dashboards",
    "search_dashboards",
]
