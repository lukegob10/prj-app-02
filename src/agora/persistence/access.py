"""Project-scoped entitlement policy and grant lifecycle services.

The functions in this module are the shared policy boundary for project
metadata, publication, and isolated artifact delivery.  A ViewerGrant is an
immutable epoch: revocation closes the epoch, and a later grant creates a new
row.  No administrator, group, or firm-wide role is consulted here.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from agora.persistence.models import AuditEvent, Dashboard, User, ViewerGrant
from agora.persistence.names import InvalidSoeid, canonicalize_soeid
from agora.persistence.querying import get_one_or_none

_GENERIC_ACCESS_FAILURE = "project access is not available"


class GrantRejection(StrEnum):
    """Stable, UI-safe reasons why a viewer grant target was rejected."""

    INVALID_SOEID = "invalid_soeid"
    UNKNOWN_USER = "unknown_user"
    DISABLED_USER = "disabled_user"
    SELF_GRANT = "self_grant"
    ALREADY_GRANTED = "already_granted"


class GrantViewerRejected(RuntimeError):
    """Raised when a target cannot receive a new active viewer grant."""

    def __init__(self, reason: GrantRejection) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ProjectAccessDenied(RuntimeError):
    """Raised for missing, non-owned, or inactive project-management callers."""


def grant_project_viewer(
    *,
    dashboard_id: UUID,
    actor_id: UUID,
    target_soeid: str,
) -> ViewerGrant:
    """Open one retained ViewerGrant epoch for a canonical active SOEID.

    The dashboard and actor are locked before target validation.  This keeps
    concurrent grant attempts for one project serialized in the normal path;
    the Oracle active-only unique index remains the final race boundary.  The
    nested atomic block is intentional: if another transaction wins the
    unique-index race, the outer durable transaction remains usable for the
    typed duplicate response.
    """
    try:
        canonical = canonicalize_soeid(target_soeid)
    except (AttributeError, InvalidSoeid, TypeError) as error:
        raise GrantViewerRejected(GrantRejection.INVALID_SOEID) from error

    with transaction.atomic(durable=True):
        dashboard, actor = _lock_active_owner(dashboard_id=dashboard_id, actor_id=actor_id)
        if dashboard.state == Dashboard.State.ARCHIVED:
            # Archived projects retain their access history, but are read-only:
            # an owner may close an existing epoch, not open a new one.
            raise ProjectAccessDenied(_GENERIC_ACCESS_FAILURE)
        target = get_one_or_none(User.objects.select_for_update().filter(soeid=canonical))
        if target is None:
            raise GrantViewerRejected(GrantRejection.UNKNOWN_USER)
        if not target.is_active:
            raise GrantViewerRejected(GrantRejection.DISABLED_USER)
        if target.id == actor.id:
            raise GrantViewerRejected(GrantRejection.SELF_GRANT)
        if has_active_viewer_grant(dashboard_id=dashboard.id, viewer_id=target.id):
            raise GrantViewerRejected(GrantRejection.ALREADY_GRANTED)

        try:
            with transaction.atomic():
                grant = ViewerGrant.objects.create(
                    dashboard=dashboard,
                    viewer=target,
                    created_by=actor,
                )
        except IntegrityError as error:
            # The function-based unique index is authoritative.  Re-query only
            # after rolling back to the savepoint so a losing insert cannot
            # poison the surrounding transaction.
            if has_active_viewer_grant(dashboard_id=dashboard.id, viewer_id=target.id):
                raise GrantViewerRejected(GrantRejection.ALREADY_GRANTED) from error
            raise

        _record_grant_audit("grant.created", actor=actor, dashboard=dashboard, grant=grant)
        return grant


def revoke_project_viewer(
    *,
    dashboard_id: UUID,
    grant_id: UUID,
    actor_id: UUID,
) -> ViewerGrant:
    """Close one grant epoch, returning the same retained row on retries.

    A revoked row is never deleted or reopened.  Locking the dashboard, actor,
    and grant makes retries idempotent while the database trigger protects the
    same invariant from bulk SQL and other model bypasses.
    """
    with transaction.atomic(durable=True):
        dashboard, actor = _lock_active_owner(dashboard_id=dashboard_id, actor_id=actor_id)
        grant = get_one_or_none(
            ViewerGrant.objects.select_for_update().filter(
                id=grant_id,
                dashboard_id=dashboard.id,
            )
        )
        if grant is None:
            raise ProjectAccessDenied(_GENERIC_ACCESS_FAILURE)
        if grant.revoked_at is not None:
            return grant

        grant.revoked_at = timezone.now()
        grant.revoked_by = actor
        grant.save(update_fields=("revoked_at", "revoked_by"))
        _record_grant_audit("grant.revoked", actor=actor, dashboard=dashboard, grant=grant)
        return grant


def active_viewer_grant(*, dashboard_id: UUID, viewer_id: UUID) -> ViewerGrant | None:
    """Resolve the one active epoch for an exact project/viewer scope."""
    return (
        ViewerGrant.objects.filter(
            dashboard_id=dashboard_id,
            viewer_id=viewer_id,
            revoked_at__isnull=True,
        )
        .order_by("id")
        .first()
    )


def has_active_viewer_grant(*, dashboard_id: UUID, viewer_id: UUID) -> bool:
    """Check an exact project/viewer entitlement with one bounded indexed query."""
    return ViewerGrant.objects.filter(
        dashboard_id=dashboard_id,
        viewer_id=viewer_id,
        revoked_at__isnull=True,
    ).exists()


def user_can_view_published(
    *,
    dashboard: Dashboard,
    viewer: User,
    viewer_grant: ViewerGrant | None = None,
) -> bool:
    """Return whether ``viewer`` may access the pinned published revision.

    Ownership is an implicit full-control relationship.  Every other viewer
    must present an active grant for this exact Dashboard.  Supplying a grant
    object lets a caller reuse an already-fetched epoch; its scope is checked
    before it is trusted, so a grant from another project cannot widen access.
    """
    if not viewer.is_active:
        return False
    if dashboard.state != Dashboard.State.PUBLISHED or dashboard.published_revision_id is None:
        return False
    if dashboard.owner_id == viewer.id:
        return True
    if viewer_grant is not None:
        return (
            viewer_grant.dashboard_id == dashboard.id
            and viewer_grant.viewer_id == viewer.id
            and viewer_grant.revoked_at is None
        )
    return has_active_viewer_grant(dashboard_id=dashboard.id, viewer_id=viewer.id)


def _lock_active_owner(*, dashboard_id: UUID, actor_id: UUID) -> tuple[Dashboard, User]:
    """Resolve and lock an active owner without exposing deleted projects."""
    dashboard = get_one_or_none(
        Dashboard.objects.select_for_update()
        .filter(id=dashboard_id)
        .exclude(state=Dashboard.State.DELETED)
    )
    if dashboard is None:
        raise ProjectAccessDenied(_GENERIC_ACCESS_FAILURE)
    actor = get_one_or_none(User.objects.select_for_update().filter(id=actor_id, is_active=True))
    if actor is None or dashboard.owner_id != actor.id:
        raise ProjectAccessDenied(_GENERIC_ACCESS_FAILURE)
    return dashboard, actor


def _record_grant_audit(
    event_type: str,
    *,
    actor: User,
    dashboard: Dashboard,
    grant: ViewerGrant,
) -> None:
    """Record a content-free, append-only grant lifecycle event."""
    AuditEvent.objects.create(
        event_type=event_type,
        actor=actor,
        dashboard=dashboard,
        target_user_id=grant.viewer_id,
        metadata={"grant_id": str(grant.id)},
    )


__all__ = [
    "GrantRejection",
    "GrantViewerRejected",
    "ProjectAccessDenied",
    "active_viewer_grant",
    "grant_project_viewer",
    "has_active_viewer_grant",
    "revoke_project_viewer",
    "user_can_view_published",
]
