"""Owner and viewer queries for the portal project experience."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, QuerySet

from agora.persistence.models import Artifact, AuditEvent, Dashboard, Revision, User, ViewerGrant
from agora.persistence.querying import get_one_or_none


class ProjectOwnerUnavailable(RuntimeError):
    """Raised when a project owner is missing or no longer active."""


def owned_projects(owner_id: UUID) -> QuerySet[Dashboard]:
    """Return retained owner-visible projects in most-recently-updated order."""
    return (
        Dashboard.objects.filter(owner_id=owner_id, owner__is_active=True)
        .exclude(state=Dashboard.State.DELETED)
        .select_related("latest_revision", "published_revision")
        .order_by("-updated_at", "name", "id")
    )


def shared_projects(viewer_id: UUID) -> QuerySet[Dashboard]:
    """Return only currently published projects actively granted to the viewer."""
    active_grant = ViewerGrant.objects.filter(
        dashboard_id=OuterRef("pk"),
        viewer_id=viewer_id,
        viewer__is_active=True,
        revoked_at__isnull=True,
    )
    return (
        Dashboard.objects.filter(
            state=Dashboard.State.PUBLISHED,
            published_revision__isnull=False,
        )
        .filter(Exists(active_grant))
        .select_related("owner", "published_revision")
        .order_by("-updated_at", "name", "id")
    )


def manageable_project(project_id: UUID, owner_id: UUID) -> Dashboard | None:
    """Resolve an owner-management route without exposing another owner's project."""
    return (
        Dashboard.objects.filter(
            id=project_id,
            owner_id=owner_id,
            owner__is_active=True,
        )
        .exclude(state=Dashboard.State.DELETED)
        .select_related("latest_revision", "published_revision")
        .first()
    )


def project_revisions(project_id: UUID, owner_id: UUID) -> QuerySet[Revision]:
    """Return an owner's revisions lazily, newest first.

    Artifact metadata is prefetched lazily with the queryset. Callers must apply a bounded slice
    (or paginator page) before evaluation so the prefetch follows that page rather than the full
    retained revision history.
    """
    return (
        Revision.objects.filter(
            dashboard_id=project_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
        )
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .select_related("created_by")
        .prefetch_related(
            Prefetch(
                "artifacts",
                queryset=Artifact.objects.order_by("kind", "logical_name", "id"),
            )
        )
        .order_by("-number", "-created_at", "-id")
    )


def project_active_grants(project_id: UUID, owner_id: UUID) -> QuerySet[ViewerGrant]:
    """Return current viewer grant epochs for an owned project in stable order."""
    return (
        ViewerGrant.objects.filter(
            dashboard_id=project_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
            revoked_at__isnull=True,
        )
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .select_related("viewer", "created_by")
        .order_by("-created_at", "-id")
    )


def project_grant_history(project_id: UUID, owner_id: UUID) -> QuerySet[ViewerGrant]:
    """Return retained revoked grant epochs, newest revocation first."""
    return (
        ViewerGrant.objects.filter(
            dashboard_id=project_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
            revoked_at__isnull=False,
        )
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .select_related("viewer", "created_by", "revoked_by")
        .order_by("-revoked_at", "-id")
    )


def project_effective_viewer_count(project_id: UUID, owner_id: UUID) -> int:
    """Count active users with an unrevoked grant in the database."""
    return (
        ViewerGrant.objects.filter(
            dashboard_id=project_id,
            dashboard__owner_id=owner_id,
            dashboard__owner__is_active=True,
            viewer__is_active=True,
            revoked_at__isnull=True,
        )
        .exclude(dashboard__state=Dashboard.State.DELETED)
        .count()
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
        owner = get_one_or_none(
            User.objects.select_for_update().filter(id=owner_id, is_active=True)
        )
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
    "project_active_grants",
    "project_effective_viewer_count",
    "project_grant_history",
    "project_revisions",
    "shared_projects",
    "visible_project",
]
