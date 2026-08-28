"""Lazy, owner-scoped query interfaces for bounded stewardship pages."""

from __future__ import annotations

from uuid import UUID

from django.db.models import QuerySet

from agora.persistence.models import AccessRequest, Dashboard


def owner_pending_access_requests(
    *,
    dashboard_id: UUID,
    owner_id: UUID,
) -> QuerySet[AccessRequest]:
    """Return a lazy pending-only queue suitable for signed keyset pagination."""

    return (
        AccessRequest.objects.filter(
            dashboard_id=dashboard_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
            status=AccessRequest.Status.PENDING,
        )
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .select_related("requester")
        .only(
            "id",
            "dashboard",
            "requester",
            "status",
            "message",
            "requested_at",
            "requester__id",
            "requester__soeid",
            "requester__is_active",
        )
        .order_by("-requested_at", "-id")
    )


__all__ = ["owner_pending_access_requests"]
