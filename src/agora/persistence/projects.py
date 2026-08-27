"""Owner and viewer queries for the portal project experience."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet

from agora.persistence.models import AuditEvent, Dashboard, User


class ProjectOwnerUnavailable(RuntimeError):
    """Raised when a project owner is missing or no longer active."""


def owned_projects(owner_id: UUID) -> QuerySet[Dashboard]:
    """Return retained owner-visible projects in most-recently-updated order."""
    return (
        Dashboard.objects.filter(owner_id=owner_id)
        .exclude(state=Dashboard.State.DELETED)
        .select_related("latest_revision", "published_revision")
        .order_by("-updated_at", "name", "id")
    )


def shared_projects(viewer_id: UUID) -> QuerySet[Dashboard]:
    """Return only currently published projects actively granted to the viewer."""
    return (
        Dashboard.objects.filter(
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
            viewer_grants__viewer_id=viewer_id,
            viewer_grants__revoked_at__isnull=True,
        )
        .select_related("owner", "published_revision")
        .order_by("-updated_at", "name", "id")
        .distinct()
    )


def manageable_project(project_id: UUID, owner_id: UUID) -> Dashboard | None:
    """Resolve an owner-management route without exposing another owner's project."""
    return (
        owned_projects(owner_id)
        .filter(id=project_id)
        .prefetch_related("revisions__artifacts", "viewer_grants")
        .first()
    )


def visible_project(project_id: UUID, viewer_id: UUID) -> tuple[Dashboard, bool] | None:
    """Resolve either an owned project or an actively shared published project."""
    owned = manageable_project(project_id, viewer_id)
    if owned is not None:
        return owned, True
    shared = (
        shared_projects(viewer_id)
        .filter(id=project_id)
        .prefetch_related("published_revision__artifacts")
        .first()
    )
    if shared is None:
        return None
    return shared, False


def create_project(*, owner_id: UUID, name: str, description: str = "") -> Dashboard:
    """Create one private project without accepting protected lifecycle fields."""
    with transaction.atomic(durable=True):
        owner = User.objects.select_for_update().filter(id=owner_id, is_active=True).first()
        if owner is None:
            raise ProjectOwnerUnavailable
        project = Dashboard(owner=owner, name=name.strip(), description=description.strip())
        project.save()
        AuditEvent.objects.create(
            event_type="dashboard.created",
            actor=owner,
            dashboard=project,
        )
        return project


__all__ = [
    "ProjectOwnerUnavailable",
    "create_project",
    "manageable_project",
    "owned_projects",
    "shared_projects",
    "visible_project",
]
