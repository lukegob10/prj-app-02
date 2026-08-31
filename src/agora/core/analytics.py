"""Authorized-open capture, bounded aggregation, retention, and rollup reads.

The raw model is intentionally imported under a private name and omitted from
``__all__``.  Portal/admin code consumes only the bounded aggregate queries in
this module; capture and jobs are wired from the authorization/operations
boundaries, never from page handlers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import QuerySet, Subquery
from django.utils import timezone

from agora.core.enhancements import EnhancementAccessDenied
from agora.core.models import (
    AnalyticsPipelineCheckpoint,
    Dashboard,
    DashboardOpenDaily,
    DashboardOpenSnapshot,
    DashboardViewerOpenSummary,
    DashboardViewerState,
    RenderAuthorization,
    ViewerGrant,
)
from agora.core.models import (
    AuthorizedOpen as _AuthorizedOpen,
)
from agora.core.querying import get_one_or_none

ANALYTICS_PIPELINE_KEY: Final = "authorized_opens_v1"
AUTHORIZED_OPEN_RETENTION_DAYS: Final = 90
DEFAULT_ANALYTICS_BATCH_SIZE: Final = 500
MAX_ANALYTICS_BATCH_SIZE: Final = 1000
MAX_ANALYTICS_READ_LIMIT: Final = 366
MAX_POPULAR_CANDIDATE_GRANTS: Final = 100


@dataclass(frozen=True, slots=True)
class AnalyticsAggregationResult:
    """Outcome of one bounded rollup batch."""

    processed: int
    last_completed_open_id: int


@dataclass(frozen=True, slots=True)
class AnalyticsPurgeResult:
    """Outcome of one bounded raw-retention batch."""

    removed: int


def capture_authorized_open(
    *,
    authorization_id: UUID,
    occurred_at: datetime | None = None,
) -> bool:
    """Capture exactly one successful published-view authorization.

    ``occurred_at`` is accepted for a stable job/test call signature but cannot
    override the metric timestamp: the immutable RenderAuthorization creation
    time is authoritative.
    """
    del occurred_at
    with transaction.atomic():
        authorization = get_one_or_none(
            RenderAuthorization.objects.select_for_update(of=("self",))
            .select_related("dashboard", "viewer", "revision", "viewer_grant")
            .filter(id=authorization_id)
        )
        if authorization is None:
            raise EnhancementAccessDenied("authorized open is not available")
        if authorization.authorized_open_captured_at is not None:
            return False
        if not _authorization_can_source_open(authorization):
            raise EnhancementAccessDenied("authorized open is not available")
        assert authorization.publication_version is not None
        try:
            with transaction.atomic():
                _AuthorizedOpen.objects.create(
                    source_authorization=authorization,
                    dashboard_id=authorization.dashboard_id,
                    viewer_id=authorization.viewer_id,
                    revision_id=authorization.revision_id,
                    publication_version=authorization.publication_version,
                    opened_at=authorization.created_at,
                )
        except IntegrityError:
            return False
        RenderAuthorization.objects.filter(
            id=authorization.id,
            authorized_open_captured_at__isnull=True,
        ).update(authorized_open_captured_at=authorization.created_at)
        authorization.authorized_open_captured_at = authorization.created_at
        _advance_viewer_state(authorization)
        return True


def aggregate_authorized_opens(
    *,
    through: datetime,
    batch_size: int = DEFAULT_ANALYTICS_BATCH_SIZE,
) -> AnalyticsAggregationResult:
    """Aggregate at most ``batch_size`` unprocessed source rows in one transaction."""
    bounded = _bounded_batch_size(batch_size)
    aggregated_at = timezone.now()
    with transaction.atomic(durable=True):
        checkpoint = _locked_checkpoint()
        candidate_open_ids = (
            _AuthorizedOpen.objects.filter(
                aggregated_at__isnull=True,
                opened_at__lte=through,
            )
            .order_by("opened_at", "id")
            .values("id")[:bounded]
        )
        opens = list(
            _AuthorizedOpen.objects.select_for_update()
            .filter(
                id__in=Subquery(candidate_open_ids),
                aggregated_at__isnull=True,
            )
            .order_by("opened_at", "id")
        )
        if not opens:
            return AnalyticsAggregationResult(
                processed=0,
                last_completed_open_id=checkpoint.last_completed_open_id,
            )

        daily_counts: Counter[tuple[UUID, date]] = Counter()
        viewer_events: defaultdict[tuple[UUID, UUID], list[_AuthorizedOpen]] = defaultdict(list)
        dashboard_events: defaultdict[UUID, list[_AuthorizedOpen]] = defaultdict(list)
        for opened in opens:
            daily_counts[(opened.dashboard_id, opened.opened_at.date())] += 1
            viewer_events[(opened.dashboard_id, opened.viewer_id)].append(opened)
            dashboard_events[opened.dashboard_id].append(opened)

        for (dashboard_id, day), increment in daily_counts.items():
            rollup = get_one_or_none(
                DashboardOpenDaily.objects.select_for_update().filter(
                    dashboard_id=dashboard_id,
                    day=day,
                )
            )
            if rollup is None:
                DashboardOpenDaily.objects.create(
                    dashboard_id=dashboard_id,
                    day=day,
                    authorized_open_count=increment,
                )
            else:
                rollup.authorized_open_count += increment
                rollup.save(update_fields=("authorized_open_count",))

        for (dashboard_id, viewer_id), events in viewer_events.items():
            first_opened_at = min(event.opened_at for event in events)
            last_opened_at = max(event.opened_at for event in events)
            summary = get_one_or_none(
                DashboardViewerOpenSummary.objects.select_for_update().filter(
                    dashboard_id=dashboard_id,
                    viewer_id=viewer_id,
                )
            )
            if summary is None:
                DashboardViewerOpenSummary.objects.create(
                    dashboard_id=dashboard_id,
                    viewer_id=viewer_id,
                    authorized_open_count=len(events),
                    first_opened_at=first_opened_at,
                    last_opened_at=last_opened_at,
                )
            else:
                summary.authorized_open_count += len(events)
                summary.first_opened_at = min(summary.first_opened_at, first_opened_at)
                summary.last_opened_at = max(summary.last_opened_at, last_opened_at)
                summary.save(
                    update_fields=(
                        "authorized_open_count",
                        "first_opened_at",
                        "last_opened_at",
                    )
                )

        for dashboard_id, events in dashboard_events.items():
            last_opened_at = max(event.opened_at for event in events)
            through_open_id = max(event.id for event in events)
            snapshot = get_one_or_none(
                DashboardOpenSnapshot.objects.select_for_update().filter(dashboard_id=dashboard_id)
            )
            if snapshot is None:
                DashboardOpenSnapshot.objects.create(
                    dashboard_id=dashboard_id,
                    authorized_open_count=len(events),
                    last_opened_at=last_opened_at,
                    captured_through_open_id=through_open_id,
                )
            else:
                snapshot.authorized_open_count += len(events)
                snapshot.last_opened_at = max(snapshot.last_opened_at, last_opened_at)
                snapshot.captured_through_open_id = max(
                    snapshot.captured_through_open_id,
                    through_open_id,
                )
                snapshot.save(
                    update_fields=(
                        "authorized_open_count",
                        "last_opened_at",
                        "captured_through_open_id",
                        "updated_at",
                    )
                )

        open_ids = [opened.id for opened in opens]
        _AuthorizedOpen.objects.filter(id__in=open_ids, aggregated_at__isnull=True).update(
            aggregated_at=aggregated_at
        )
        checkpoint.last_completed_open_id = max(
            checkpoint.last_completed_open_id,
            max(open_ids),
        )
        checkpoint.save(update_fields=("last_completed_open_id", "updated_at"))
        return AnalyticsAggregationResult(
            processed=len(opens),
            last_completed_open_id=checkpoint.last_completed_open_id,
        )


def purge_authorized_opens(
    *,
    before: datetime,
    batch_size: int = DEFAULT_ANALYTICS_BATCH_SIZE,
) -> AnalyticsPurgeResult:
    """Delete at most one batch of processed, checkpointed rows older than 90 days."""
    bounded = _bounded_batch_size(batch_size)
    if before > timezone.now() - timedelta(days=AUTHORIZED_OPEN_RETENTION_DAYS):
        raise ValueError("authorized opens must be retained for at least 90 days")
    with transaction.atomic(durable=True):
        checkpoint = get_one_or_none(
            AnalyticsPipelineCheckpoint.objects.select_for_update().filter(
                pipeline_key=ANALYTICS_PIPELINE_KEY
            )
        )
        if checkpoint is None:
            return AnalyticsPurgeResult(removed=0)
        candidate_open_ids = (
            _AuthorizedOpen.objects.filter(opened_at__lt=before)
            .order_by("opened_at", "id")
            .values("id")[:bounded]
        )
        removable_ids = list(
            _AuthorizedOpen.objects.filter(
                id__in=Subquery(candidate_open_ids),
                aggregated_at__isnull=False,
                id__lte=checkpoint.last_completed_open_id,
            )
            .order_by("opened_at", "id")
            .values_list("id", flat=True)
        )
        if not removable_ids:
            return AnalyticsPurgeResult(removed=0)
        _AuthorizedOpen.objects.filter(id__in=removable_ids).delete()
        return AnalyticsPurgeResult(removed=len(removable_ids))


def dashboard_daily_authorized_opens(
    *,
    dashboard_id: UUID,
    owner_id: UUID,
    start: date,
    end: date,
    limit: int = MAX_ANALYTICS_READ_LIMIT,
) -> QuerySet[DashboardOpenDaily]:
    """Return an owner-scoped, bounded daily rollup range."""
    bounded = _bounded_read_limit(limit)
    if end < start:
        raise ValueError("analytics date range is invalid")
    return DashboardOpenDaily.objects.filter(
        dashboard_id=dashboard_id,
        dashboard__owner_id=owner_id,
        dashboard__owner__is_active=True,
        day__gte=start,
        day__lte=end,
    ).order_by("-day", "-id")[:bounded]


def dashboard_viewer_authorized_open_summaries(
    *,
    dashboard_id: UUID,
    owner_id: UUID,
    limit: int = 100,
) -> QuerySet[DashboardViewerOpenSummary]:
    """Return a bounded owner-only viewer summary, never raw source rows."""
    bounded = _bounded_read_limit(limit, maximum=100)
    return (
        DashboardViewerOpenSummary.objects.filter(
            dashboard_id=dashboard_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
        )
        .select_related("viewer")
        .order_by("-authorized_open_count", "viewer_id")[:bounded]
    )


def popular_authorized_dashboard_snapshots(
    *,
    principal_id: UUID,
    limit: int = 25,
) -> QuerySet[DashboardOpenSnapshot]:
    """Rank a bounded recent window of currently granted Published dashboards."""
    bounded = _bounded_read_limit(limit, maximum=100)
    candidate_dashboard_ids = (
        ViewerGrant.objects.filter(
            viewer_id=principal_id,
            viewer__is_active=True,
            revoked_at__isnull=True,
        )
        .order_by("-created_at", "-id")
        .values("dashboard_id")[:MAX_POPULAR_CANDIDATE_GRANTS]
    )
    return (
        DashboardOpenSnapshot.objects.filter(
            dashboard_id__in=Subquery(candidate_dashboard_ids),
            dashboard__state=Dashboard.State.PUBLISHED,
            dashboard__published_revision__isnull=False,
        )
        .select_related("dashboard")
        .order_by("-authorized_open_count", "dashboard_id")[:bounded]
    )


def _authorization_can_source_open(authorization: RenderAuthorization) -> bool:
    dashboard = authorization.dashboard
    viewer = authorization.viewer
    if (
        authorization.audience != RenderAuthorization.Audience.VIEWER
        or authorization.publication_version is None
        or authorization.publication_version <= 0
        or authorization.revoked_at is not None
        or not viewer.is_active
        or viewer.auth_version != authorization.viewer_auth_version
        or dashboard.state != Dashboard.State.PUBLISHED
        or dashboard.published_revision_id != authorization.revision_id
        or dashboard.publication_version != authorization.publication_version
    ):
        return False
    if dashboard.owner_id == viewer.id:
        return (
            authorization.viewer_grant_id is None
            and authorization.owner_transfer_epoch_id == dashboard.last_ownership_transfer_id
        )
    grant = authorization.viewer_grant
    return (
        authorization.owner_transfer_epoch_id is None
        and grant is not None
        and grant.dashboard_id == dashboard.id
        and grant.viewer_id == viewer.id
        and grant.revoked_at is None
    )


def _advance_viewer_state(authorization: RenderAuthorization) -> DashboardViewerState:
    """Advance compact view state only for the validated source authorization."""
    assert authorization.publication_version is not None
    state = get_one_or_none(
        DashboardViewerState.objects.select_for_update().filter(
            user_id=authorization.viewer_id,
            dashboard_id=authorization.dashboard_id,
        )
    )
    if state is None:
        try:
            with transaction.atomic():
                return DashboardViewerState.objects.create(
                    user=authorization.viewer,
                    dashboard=authorization.dashboard,
                    last_viewed_at=authorization.created_at,
                    seen_publication_version=authorization.publication_version,
                )
        except IntegrityError:
            state = DashboardViewerState.objects.select_for_update().get(
                user_id=authorization.viewer_id,
                dashboard_id=authorization.dashboard_id,
            )
    next_viewed_at = max(state.last_viewed_at, authorization.created_at)
    next_version = max(
        state.seen_publication_version,
        authorization.publication_version,
    )
    if next_viewed_at != state.last_viewed_at or next_version != state.seen_publication_version:
        state.last_viewed_at = next_viewed_at
        state.seen_publication_version = next_version
        state.save(update_fields=("last_viewed_at", "seen_publication_version"))
    return state


def _locked_checkpoint() -> AnalyticsPipelineCheckpoint:
    checkpoint = get_one_or_none(
        AnalyticsPipelineCheckpoint.objects.select_for_update().filter(
            pipeline_key=ANALYTICS_PIPELINE_KEY
        )
    )
    if checkpoint is not None:
        return checkpoint
    try:
        with transaction.atomic():
            return AnalyticsPipelineCheckpoint.objects.create(pipeline_key=ANALYTICS_PIPELINE_KEY)
    except IntegrityError:
        return AnalyticsPipelineCheckpoint.objects.select_for_update().get(
            pipeline_key=ANALYTICS_PIPELINE_KEY
        )


def _bounded_batch_size(batch_size: int) -> int:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_ANALYTICS_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_ANALYTICS_BATCH_SIZE}")
    return batch_size


def _bounded_read_limit(limit: int, *, maximum: int = MAX_ANALYTICS_READ_LIMIT) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


__all__ = [
    "AnalyticsAggregationResult",
    "AnalyticsPurgeResult",
    "aggregate_authorized_opens",
    "capture_authorized_open",
    "dashboard_daily_authorized_opens",
    "dashboard_viewer_authorized_open_summaries",
    "popular_authorized_dashboard_snapshots",
    "purge_authorized_opens",
]
