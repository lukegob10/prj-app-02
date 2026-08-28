"""Bounded, index-aligned read interfaces for later enhancement pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from django.db.models import BooleanField, Case, Exists, F, OuterRef, Q, QuerySet, Subquery, When

from agora.persistence.models import (
    AccessRequest,
    Dashboard,
    DashboardFavorite,
    DashboardTag,
    DashboardViewerState,
    ViewerGrant,
)
from agora.persistence.names import InvalidDashboardTag, normalize_dashboard_tag

DEFAULT_PAGE_LIMIT: Final = 25
MAX_PAGE_LIMIT: Final = 100
MAX_AUTHORIZATION_CANDIDATES: Final = 100


@dataclass(frozen=True, slots=True)
class DescendingTimeCursor:
    """Exclusive keyset cursor for a timestamp/id descending index."""

    timestamp: datetime
    row_id: int


@dataclass(frozen=True, slots=True)
class AscendingTimeCursor:
    """Exclusive keyset cursor for a timestamp/id ascending index."""

    timestamp: datetime
    row_id: UUID


def dashboard_tags(
    *,
    dashboard_id: UUID,
    principal_id: UUID,
    limit: int = 5,
) -> QuerySet[DashboardTag]:
    """Return at most five tags only when the principal can currently see the dashboard."""
    bounded = _bounded_limit(limit, maximum=5)
    active_grant = _active_grant_exists(
        dashboard_outer_ref="dashboard_id",
        viewer_id=principal_id,
    )
    return (
        DashboardTag.objects.filter(dashboard_id=dashboard_id)
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .annotate(_has_active_grant=Exists(active_grant))
        .filter(
            Q(dashboard__owner_id=principal_id, dashboard__owner__is_active=True)
            | Q(
                _has_active_grant=True,
                dashboard__state=Dashboard.State.PUBLISHED,
                dashboard__published_revision__isnull=False,
            )
        )
        .order_by("slot", "id")[:bounded]
    )


def dashboards_by_tag_key(
    *,
    principal_id: UUID,
    tag: str,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> QuerySet[Dashboard]:
    """Return an authorized top-N snapshot for one canonical tag key."""
    bounded = _bounded_limit(limit)
    try:
        tag_key = normalize_dashboard_tag(tag).key
    except InvalidDashboardTag as error:
        raise ValueError(str(error)) from error
    active_grant = _active_grant_exists(dashboard_outer_ref="pk", viewer_id=principal_id)
    candidate_dashboard_ids = (
        DashboardTag.objects.filter(key=tag_key)
        .order_by("dashboard_id")
        .values("dashboard_id")[:MAX_AUTHORIZATION_CANDIDATES]
    )
    return (
        Dashboard.objects.filter(
            id__in=Subquery(candidate_dashboard_ids),
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
        )
        .annotate(_has_active_grant=Exists(active_grant))
        .filter(Q(owner_id=principal_id, owner__is_active=True) | Q(_has_active_grant=True))
        .select_related("owner", "published_revision")
        .order_by("id")[:bounded]
    )


def recent_favorite_dashboards(
    *,
    user_id: UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    before: DescendingTimeCursor | None = None,
) -> QuerySet[Dashboard]:
    """Return an authorized top-N favorite snapshot without leaking revoked shares."""
    bounded = _bounded_limit(limit)
    active_grant = _active_grant_exists(dashboard_outer_ref="pk", viewer_id=user_id)
    candidates = DashboardFavorite.objects.filter(user_id=user_id)
    if before is not None:
        candidates = candidates.filter(
            Q(created_at__lt=before.timestamp)
            | Q(created_at=before.timestamp, id__lt=before.row_id)
        )
    candidate_dashboard_ids = candidates.order_by("-created_at", "-id").values("dashboard_id")[
        :MAX_AUTHORIZATION_CANDIDATES
    ]
    queryset = (
        Dashboard.objects.filter(
            id__in=Subquery(candidate_dashboard_ids),
            favorites__user_id=user_id,
            favorites__user__is_active=True,
        )
        .exclude(state=Dashboard.State.DELETED)
        .annotate(_has_active_grant=Exists(active_grant))
        .filter(
            Q(owner_id=user_id)
            | Q(
                _has_active_grant=True,
                state=Dashboard.State.PUBLISHED,
                published_revision__isnull=False,
            )
        )
    )
    return queryset.select_related("owner", "published_revision").order_by(
        "-favorites__created_at",
        "-favorites__id",
    )[:bounded]


def recently_viewed_dashboards(
    *,
    user_id: UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    before: DescendingTimeCursor | None = None,
) -> QuerySet[Dashboard]:
    """Return an authorized recent-view snapshot with a version-derived New annotation."""
    bounded = _bounded_limit(limit)
    active_grant = _active_grant_exists(dashboard_outer_ref="pk", viewer_id=user_id)
    candidates = DashboardViewerState.objects.filter(user_id=user_id)
    if before is not None:
        candidates = candidates.filter(
            Q(last_viewed_at__lt=before.timestamp)
            | Q(last_viewed_at=before.timestamp, id__lt=before.row_id)
        )
    candidate_dashboard_ids = candidates.order_by("-last_viewed_at", "-id").values("dashboard_id")[
        :MAX_AUTHORIZATION_CANDIDATES
    ]
    queryset = (
        Dashboard.objects.filter(
            id__in=Subquery(candidate_dashboard_ids),
            viewer_states__user_id=user_id,
            viewer_states__user__is_active=True,
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
        )
        .annotate(_has_active_grant=Exists(active_grant))
        .filter(Q(owner_id=user_id) | Q(_has_active_grant=True))
        .annotate(
            has_new_publication=Case(
                When(
                    publication_version__gt=F("viewer_states__seen_publication_version"),
                    then=True,
                ),
                default=False,
                output_field=BooleanField(),
            )
        )
    )
    return queryset.select_related("owner", "published_revision").order_by(
        "-viewer_states__last_viewed_at",
        "-viewer_states__id",
    )[:bounded]


def pending_access_requests(
    *,
    owner_id: UUID,
    dashboard_id: UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    before: DescendingTimeCursor | None = None,
) -> QuerySet[AccessRequest]:
    """Return one bounded, dashboard-scoped owner queue on its covering index."""
    bounded = _bounded_limit(limit)
    queryset = AccessRequest.objects.filter(
        dashboard_id=dashboard_id,
        dashboard__owner_id=owner_id,
        dashboard__owner__is_active=True,
        status=AccessRequest.Status.PENDING,
    )
    if before is not None:
        queryset = queryset.filter(
            Q(requested_at__lt=before.timestamp)
            | Q(requested_at=before.timestamp, id__lt=before.row_id)
        )
    return queryset.select_related("dashboard", "requester").order_by(
        "-requested_at",
        "-id",
    )[:bounded]


def stale_owned_dashboards(
    *,
    owner_id: UUID,
    as_of: datetime,
    limit: int = DEFAULT_PAGE_LIMIT,
    after: AscendingTimeCursor | None = None,
) -> QuerySet[Dashboard]:
    """Return a bounded read-time staleness result; no stored flag is consulted."""
    bounded = _bounded_limit(limit)
    candidates = Dashboard.objects.filter(
        owner_id=owner_id,
        stale_after__isnull=False,
        stale_after__lte=as_of,
    )
    if after is not None:
        candidates = candidates.filter(
            Q(stale_after__gt=after.timestamp) | Q(stale_after=after.timestamp, id__gt=after.row_id)
        )
    candidate_ids = candidates.order_by("stale_after", "id").values("id")[
        :MAX_AUTHORIZATION_CANDIDATES
    ]
    queryset = Dashboard.objects.filter(
        id__in=Subquery(candidate_ids),
        owner_id=owner_id,
        owner__is_active=True,
        state=Dashboard.State.PUBLISHED,
    )
    return queryset.select_related("published_revision").order_by("stale_after", "id")[:bounded]


def _active_grant_exists(*, dashboard_outer_ref: str, viewer_id: UUID) -> QuerySet[ViewerGrant]:
    return ViewerGrant.objects.filter(
        dashboard_id=OuterRef(dashboard_outer_ref),
        viewer_id=viewer_id,
        viewer__is_active=True,
        revoked_at__isnull=True,
    )


def _bounded_limit(limit: int, *, maximum: int = MAX_PAGE_LIMIT) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


__all__ = [
    "AscendingTimeCursor",
    "DescendingTimeCursor",
    "dashboard_tags",
    "dashboards_by_tag_key",
    "pending_access_requests",
    "recent_favorite_dashboards",
    "recently_viewed_dashboards",
    "stale_owned_dashboards",
]
