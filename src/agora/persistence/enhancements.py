"""Stable domain services for Agora's enhancement foundation.

These functions are the mutation boundary consumed by later server-rendered
feature lanes.  Every operation locks one exact project/user scope, performs
bounded work, and records security-relevant lifecycle changes without exposing
uploaded content or free-form request text in audit metadata.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import Enum
from typing import Final
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from agora.persistence.models import (
    AccessRequest,
    AuditEvent,
    Dashboard,
    DashboardFavorite,
    DashboardOwnershipTransfer,
    DashboardTag,
    Revision,
    User,
    ViewerGrant,
)
from agora.persistence.names import InvalidDashboardTag, normalize_dashboard_tag
from agora.persistence.querying import get_one_or_none

MAX_EFFECTIVE_TAGS: Final = 5
MAX_PUBLICATION_NOTE_LENGTH: Final = 240
MAX_ACCESS_REQUEST_MESSAGE_LENGTH: Final = 500
MAX_FRESHNESS_INTERVAL_SECONDS: Final = 31_536_000
_GENERIC_FAILURE = "dashboard action is not available"


class EnhancementAccessDenied(RuntimeError):
    """Fail-closed result for an unauthorized or ineligible domain action."""


class EnhancementValidationError(ValueError):
    """Typed, UI-safe validation failure for an enhancement input."""


class _Unset(Enum):
    VALUE = "unset"


UNSET: Final = _Unset.VALUE


def normalize_tag_key(value: str) -> str:
    """Return the exact persisted case-insensitive tag key."""
    try:
        return normalize_dashboard_tag(value).key
    except InvalidDashboardTag as error:
        raise EnhancementValidationError(str(error)) from error


def replace_dashboard_tags(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    labels: Sequence[str],
) -> tuple[DashboardTag, ...]:
    """Atomically replace the at-most-five effective tags for one owned dashboard."""
    normalized = []
    seen: set[str] = set()
    for label in labels:
        try:
            tag = normalize_dashboard_tag(label)
        except InvalidDashboardTag as error:
            raise EnhancementValidationError(str(error)) from error
        if tag.key in seen:
            raise EnhancementValidationError("each tag must be unique after normalization")
        seen.add(tag.key)
        normalized.append(tag)
    if len(normalized) > MAX_EFFECTIVE_TAGS:
        raise EnhancementValidationError("a dashboard can have at most five tags")

    with transaction.atomic(durable=True):
        dashboard, actor = _lock_current_owner(dashboard_id=dashboard_id, actor_id=actor_id)
        if dashboard.state in {Dashboard.State.ARCHIVED, Dashboard.State.DELETED}:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        DashboardTag.objects.filter(dashboard_id=dashboard.id).delete()
        created = tuple(
            DashboardTag.objects.create(
                dashboard=dashboard,
                label=tag.display,
                key=tag.key,
                slot=slot,
            )
            for slot, tag in enumerate(normalized, start=1)
        )
        AuditEvent.objects.create(
            event_type="dashboard.tags_replaced",
            actor=actor,
            dashboard=dashboard,
            metadata={"tag_count": len(created)},
        )
        return created


def set_dashboard_favorite(
    *,
    dashboard_id: UUID,
    user_id: UUID,
    favorited: bool,
) -> DashboardFavorite | None:
    """Set or clear one personal favorite idempotently.

    Creation locks the dashboard before resolving the principal so publication,
    ownership, grant, and account changes cannot race the authorization check.
    Removing a retained shortcut remains available without treating that row as
    evidence that the dashboard is still visible.
    """
    with transaction.atomic(durable=True):
        if not favorited:
            user = get_one_or_none(
                User.objects.select_for_update().filter(id=user_id, is_active=True)
            )
            if user is None:
                raise EnhancementAccessDenied(_GENERIC_FAILURE)
            DashboardFavorite.objects.filter(user_id=user.id, dashboard_id=dashboard_id).delete()
            return None

        dashboard = get_one_or_none(
            Dashboard.objects.select_for_update().filter(
                id=dashboard_id,
                state=Dashboard.State.PUBLISHED,
                published_revision__isnull=False,
            )
        )
        user = get_one_or_none(User.objects.select_for_update().filter(id=user_id, is_active=True))
        if dashboard is None or user is None:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        if dashboard.owner_id != user.id and _active_grant(dashboard.id, user.id) is None:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        favorite, _ = DashboardFavorite.objects.get_or_create(user=user, dashboard=dashboard)
        return favorite


def request_dashboard_access(
    *,
    dashboard_id: UUID,
    requester_id: UUID,
    message: str = "",
) -> AccessRequest:
    """Create, return, or reopen the one request row for a published dashboard."""
    normalized_message = _normalize_short_text(
        message,
        maximum=MAX_ACCESS_REQUEST_MESSAGE_LENGTH,
        label="request message",
    )
    requested_at = timezone.now()
    with transaction.atomic(durable=True):
        dashboard = get_one_or_none(
            Dashboard.objects.select_for_update()
            .select_related("owner")
            .filter(
                id=dashboard_id,
                state=Dashboard.State.PUBLISHED,
                owner__is_active=True,
            )
        )
        requester = get_one_or_none(
            User.objects.select_for_update().filter(id=requester_id, is_active=True)
        )
        if (
            dashboard is None
            or requester is None
            or dashboard.owner_id == requester.id
            or _active_grant(dashboard.id, requester.id) is not None
        ):
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        access_request = get_one_or_none(
            AccessRequest.objects.select_for_update().filter(
                dashboard_id=dashboard.id,
                requester_id=requester.id,
            )
        )
        if access_request is not None and access_request.status == AccessRequest.Status.PENDING:
            return access_request
        reopened = access_request is not None
        if access_request is None:
            access_request = AccessRequest.objects.create(
                dashboard=dashboard,
                requester=requester,
                message=normalized_message,
                requested_at=requested_at,
            )
        else:
            access_request.status = AccessRequest.Status.PENDING
            access_request.message = normalized_message
            access_request.requested_at = max(
                requested_at,
                access_request.requested_at + timedelta(microseconds=1),
            )
            access_request.resolved_at = None
            access_request.resolved_by = None
            access_request.save(
                update_fields=(
                    "status",
                    "message",
                    "requested_at",
                    "resolved_at",
                    "resolved_by",
                    "updated_at",
                )
            )
        AuditEvent.objects.create(
            event_type="access.requested",
            actor=requester,
            dashboard=dashboard,
            target_user=requester,
            metadata={"request_id": access_request.id, "reopened": reopened},
        )
        return access_request


def resolve_dashboard_access_request(
    *,
    dashboard_id: UUID,
    request_id: int,
    actor_id: UUID,
    resolution: AccessRequest.Status | str,
    now: datetime | None = None,
) -> AccessRequest:
    """Resolve one pending request, atomically opening a grant on approval."""
    try:
        resolved_status = AccessRequest.Status(resolution)
    except ValueError as error:
        raise EnhancementValidationError("access-request resolution is invalid") from error
    if resolved_status == AccessRequest.Status.PENDING:
        raise EnhancementValidationError("pending is not a resolution")
    resolved_at = now or timezone.now()

    with transaction.atomic(durable=True):
        dashboard = get_one_or_none(
            Dashboard.objects.select_for_update().select_related("owner").filter(id=dashboard_id)
        )
        access_request = get_one_or_none(
            AccessRequest.objects.select_for_update()
            .select_related("requester")
            .filter(id=request_id, dashboard_id=dashboard_id)
        )
        actor = get_one_or_none(
            User.objects.select_for_update().filter(id=actor_id, is_active=True)
        )
        if dashboard is None or access_request is None or actor is None:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        expected_actor_id = (
            access_request.requester_id
            if resolved_status == AccessRequest.Status.CANCELLED
            else dashboard.owner_id
        )
        if actor.id != expected_actor_id:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        if access_request.status != AccessRequest.Status.PENDING:
            if access_request.status == resolved_status:
                return access_request
            raise EnhancementAccessDenied(_GENERIC_FAILURE)

        if resolved_status == AccessRequest.Status.APPROVED:
            if (
                dashboard.state not in {Dashboard.State.PUBLISHED, Dashboard.State.UNPUBLISHED}
                or not dashboard.owner.is_active
                or not access_request.requester.is_active
            ):
                raise EnhancementAccessDenied(_GENERIC_FAILURE)
            grant = _active_grant(dashboard.id, access_request.requester_id, lock=True)
            if grant is None:
                grant = ViewerGrant.objects.create(
                    dashboard=dashboard,
                    viewer=access_request.requester,
                    created_by=actor,
                )
                _audit_grant("grant.created", actor=actor, dashboard=dashboard, grant=grant)

        access_request.status = resolved_status
        access_request.resolved_at = resolved_at
        access_request.resolved_by = actor
        access_request.save(update_fields=("status", "resolved_at", "resolved_by", "updated_at"))
        AuditEvent.objects.create(
            event_type="access.resolved",
            actor=actor,
            dashboard=dashboard,
            target_user_id=access_request.requester_id,
            metadata={"request_id": access_request.id, "resolution": resolved_status.value},
        )
        return access_request


def transfer_dashboard_ownership(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    incoming_owner_id: UUID,
    expected_transfer_id: UUID | _Unset | None = UNSET,
    now: datetime | None = None,
) -> Dashboard:
    """Atomically transfer one dashboard while preserving every historical actor.

    ``expected_transfer_id`` binds a browser confirmation to the exact ownership epoch.  The
    comparison happens only after locking the Dashboard, so an A-to-B-to-A transfer cannot make a
    stale confirmation current again.  Existing trusted callers may omit it and still receive the
    service's current-owner and optimistic-update protections.
    """
    transferred_at = now or timezone.now()
    with transaction.atomic(durable=True):
        dashboard = get_one_or_none(Dashboard.objects.select_for_update().filter(id=dashboard_id))
        if dashboard is None or dashboard.state not in {
            Dashboard.State.DRAFT,
            Dashboard.State.PUBLISHED,
            Dashboard.State.UNPUBLISHED,
        }:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        if (
            expected_transfer_id is not UNSET
            and dashboard.last_ownership_transfer_id != expected_transfer_id
        ):
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        users = {
            user.id: user
            for user in User.objects.select_for_update()
            .filter(id__in=(actor_id, incoming_owner_id), is_active=True)
            .order_by("id")
        }
        actor = users.get(actor_id)
        incoming_owner = users.get(incoming_owner_id)
        if (
            actor is None
            or incoming_owner is None
            or actor.id != dashboard.owner_id
            or incoming_owner.id == actor.id
        ):
            raise EnhancementAccessDenied(_GENERIC_FAILURE)

        incoming_grant = _active_grant(dashboard.id, incoming_owner.id, lock=True)
        if incoming_grant is not None:
            incoming_grant.revoked_at = transferred_at
            incoming_grant.revoked_by = actor
            incoming_grant.save(update_fields=("revoked_at", "revoked_by"))
            _audit_grant(
                "grant.revoked",
                actor=actor,
                dashboard=dashboard,
                grant=incoming_grant,
            )

        incoming_request = get_one_or_none(
            AccessRequest.objects.select_for_update().filter(
                dashboard_id=dashboard.id,
                requester_id=incoming_owner.id,
                status=AccessRequest.Status.PENDING,
            )
        )
        if incoming_request is not None:
            incoming_request.status = AccessRequest.Status.APPROVED
            incoming_request.resolved_at = transferred_at
            incoming_request.resolved_by = actor
            incoming_request.save(
                update_fields=("status", "resolved_at", "resolved_by", "updated_at")
            )
            AuditEvent.objects.create(
                event_type="access.resolved",
                actor=actor,
                dashboard=dashboard,
                target_user=incoming_owner,
                metadata={
                    "request_id": incoming_request.id,
                    "resolution": AccessRequest.Status.APPROVED.value,
                },
            )

        marker = DashboardOwnershipTransfer.objects.create(
            dashboard=dashboard,
            from_owner=actor,
            to_owner=incoming_owner,
            previous_transfer_id=dashboard.last_ownership_transfer_id,
        )
        updated = Dashboard.objects.filter(
            id=dashboard.id,
            owner_id=actor.id,
            last_ownership_transfer_id=dashboard.last_ownership_transfer_id,
        ).update(
            owner_id=incoming_owner.id,
            last_ownership_transfer_id=marker.id,
            updated_at=transferred_at,
        )
        if updated != 1:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        AuditEvent.objects.create(
            event_type="dashboard.ownership_transferred",
            actor=actor,
            dashboard=dashboard,
            target_user=incoming_owner,
            metadata={"transfer_id": str(marker.id)},
        )
        dashboard.refresh_from_db()
        return dashboard


def publish_dashboard_revision(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    revision_id: UUID,
    publication_note: str = "",
    data_as_of: datetime | None = None,
    freshness_interval: timedelta | None = None,
    now: datetime | None = None,
) -> Dashboard:
    """Publish or republish one complete revision as the next immutable release version."""
    note = _normalize_short_text(
        publication_note,
        maximum=MAX_PUBLICATION_NOTE_LENGTH,
        label="publication note",
    )
    published_at = now or timezone.now()
    freshness_seconds = _freshness_seconds(freshness_interval)
    with transaction.atomic(durable=True):
        dashboard, actor = _lock_current_owner(dashboard_id=dashboard_id, actor_id=actor_id)
        if dashboard.state not in {
            Dashboard.State.DRAFT,
            Dashboard.State.PUBLISHED,
            Dashboard.State.UNPUBLISHED,
        }:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        revision = get_one_or_none(
            Revision.objects.filter(
                id=revision_id,
                dashboard_id=dashboard.id,
                artifacts_locked=True,
            )
        )
        if revision is None:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        if dashboard.last_published_at is not None and published_at < dashboard.last_published_at:
            raise EnhancementValidationError("publication time cannot move backward")
        first_publication = dashboard.first_published_at is None
        dashboard.state = Dashboard.State.PUBLISHED
        dashboard.published_revision = revision
        if first_publication:
            dashboard.first_published_at = published_at
        dashboard.publication_version += 1
        dashboard.last_published_at = published_at
        dashboard.publication_note = note
        dashboard.data_as_of = data_as_of
        _apply_freshness(
            dashboard,
            interval_seconds=freshness_seconds,
            confirmed_at=published_at,
        )
        dashboard.save(
            update_fields=(
                "state",
                "published_revision",
                "first_published_at",
                "publication_version",
                "last_published_at",
                "publication_note",
                "data_as_of",
                "freshness_interval_seconds",
                "freshness_confirmed_at",
                "stale_after",
                "updated_at",
            )
        )
        AuditEvent.objects.create(
            event_type="dashboard.published",
            actor=actor,
            dashboard=dashboard,
            revision=revision,
            metadata={
                "publication_version": dashboard.publication_version,
                "republished": not first_publication,
            },
        )
        return dashboard


def rollback_dashboard_by_republish(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    revision_id: UUID,
    publication_note: str = "",
    data_as_of: datetime | None = None,
    freshness_interval: timedelta | None = None,
    now: datetime | None = None,
) -> Dashboard:
    """Rollback safely by publishing an older retained revision as a new version."""
    return publish_dashboard_revision(
        dashboard_id=dashboard_id,
        actor_id=actor_id,
        revision_id=revision_id,
        publication_note=publication_note,
        data_as_of=data_as_of,
        freshness_interval=freshness_interval,
        now=now,
    )


def confirm_dashboard_freshness(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    freshness_interval: timedelta | None,
    data_as_of: datetime | _Unset | None = UNSET,
    now: datetime | None = None,
) -> Dashboard:
    """Replace or clear a freshness claim without creating a publication version."""
    confirmed_at = now or timezone.now()
    freshness_seconds = _freshness_seconds(freshness_interval)
    with transaction.atomic(durable=True):
        dashboard, actor = _lock_current_owner(dashboard_id=dashboard_id, actor_id=actor_id)
        if dashboard.state != Dashboard.State.PUBLISHED:
            raise EnhancementAccessDenied(_GENERIC_FAILURE)
        if data_as_of is not UNSET:
            dashboard.data_as_of = data_as_of
        _apply_freshness(
            dashboard,
            interval_seconds=freshness_seconds,
            confirmed_at=confirmed_at,
        )
        dashboard.save(
            update_fields=(
                "data_as_of",
                "freshness_interval_seconds",
                "freshness_confirmed_at",
                "stale_after",
                "updated_at",
            )
        )
        AuditEvent.objects.create(
            event_type="dashboard.freshness_confirmed",
            actor=actor,
            dashboard=dashboard,
            metadata={"freshness_provided": freshness_seconds is not None},
        )
        return dashboard


def _lock_current_owner(*, dashboard_id: UUID, actor_id: UUID) -> tuple[Dashboard, User]:
    dashboard = get_one_or_none(Dashboard.objects.select_for_update().filter(id=dashboard_id))
    actor = get_one_or_none(User.objects.select_for_update().filter(id=actor_id, is_active=True))
    if dashboard is None or actor is None or dashboard.owner_id != actor.id:
        raise EnhancementAccessDenied(_GENERIC_FAILURE)
    return dashboard, actor


def _active_grant(
    dashboard_id: UUID,
    viewer_id: UUID,
    *,
    lock: bool = False,
) -> ViewerGrant | None:
    queryset = ViewerGrant.objects.filter(
        dashboard_id=dashboard_id,
        viewer_id=viewer_id,
        revoked_at__isnull=True,
    )
    if lock:
        return get_one_or_none(queryset.select_for_update())
    return queryset.order_by("id").first()


def _freshness_seconds(interval: timedelta | None) -> int | None:
    if interval is None:
        return None
    seconds = interval.total_seconds()
    if not seconds.is_integer() or not 1 <= seconds <= MAX_FRESHNESS_INTERVAL_SECONDS:
        raise EnhancementValidationError(
            "freshness interval must be whole seconds between 1 second and 1 year"
        )
    return int(seconds)


def _apply_freshness(
    dashboard: Dashboard,
    *,
    interval_seconds: int | None,
    confirmed_at: datetime,
) -> None:
    dashboard.freshness_interval_seconds = interval_seconds
    dashboard.freshness_confirmed_at = confirmed_at if interval_seconds is not None else None
    dashboard.stale_after = (
        confirmed_at + timedelta(seconds=interval_seconds) if interval_seconds is not None else None
    )


def _normalize_short_text(value: str, *, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise EnhancementValidationError(f"{label} must be text")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise EnhancementValidationError(f"{label} cannot contain control characters")
    normalized = " ".join(value.split())
    if len(normalized) > maximum:
        raise EnhancementValidationError(f"{label} must be at most {maximum} characters")
    return normalized


def _audit_grant(
    event_type: str,
    *,
    actor: User,
    dashboard: Dashboard,
    grant: ViewerGrant,
) -> None:
    AuditEvent.objects.create(
        event_type=event_type,
        actor=actor,
        dashboard=dashboard,
        target_user_id=grant.viewer_id,
        metadata={"grant_id": str(grant.id)},
    )


__all__ = [
    "EnhancementAccessDenied",
    "EnhancementValidationError",
    "confirm_dashboard_freshness",
    "normalize_tag_key",
    "publish_dashboard_revision",
    "replace_dashboard_tags",
    "request_dashboard_access",
    "resolve_dashboard_access_request",
    "rollback_dashboard_by_republish",
    "set_dashboard_favorite",
    "transfer_dashboard_ownership",
]
