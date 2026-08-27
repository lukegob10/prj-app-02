"""Short-lived authorization shared by the portal issuer and content verifier."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from agora.persistence.models import (
    AuditEvent,
    Dashboard,
    RenderAuthorization,
    Revision,
    User,
    ViewerGrant,
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", flags=re.ASCII)
_TOKEN_BYTES = 32
_TOKEN_ATTEMPTS = 3


class RenderAuthorizationDenied(RuntimeError):
    """Fail-closed result for an invalid or no-longer-authorized render credential."""


class RenderAuthorizationUnavailable(RuntimeError):
    """Raised when a render credential cannot be issued safely."""


@dataclass(frozen=True, slots=True)
class RenderCredential:
    """The one-time-visible bearer value returned only to the trusted portal shell."""

    token: str
    audience: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedRender:
    """Fresh database-backed scope resolved by the isolated content service."""

    authorization: RenderAuthorization
    dashboard: Dashboard
    revision: Revision
    viewer: User


def issue_owner_preview(
    *,
    dashboard_id: UUID,
    revision_id: UUID,
    viewer_id: UUID,
    now: datetime | None = None,
) -> RenderCredential:
    """Issue a private preview only for the active owner and exact complete revision."""
    issued_at = now or timezone.now()
    with transaction.atomic(durable=True):
        dashboard = (
            Dashboard.objects.select_for_update()
            .filter(id=dashboard_id)
            .exclude(state__in=[Dashboard.State.ARCHIVED, Dashboard.State.DELETED])
            .first()
        )
        viewer = User.objects.select_for_update().filter(id=viewer_id, is_active=True).first()
        revision = Revision.objects.filter(
            id=revision_id,
            dashboard_id=dashboard_id,
            artifacts_locked=True,
        ).first()
        if (
            dashboard is None
            or viewer is None
            or revision is None
            or dashboard.owner_id != viewer.id
        ):
            raise RenderAuthorizationDenied("render authorization is not available")
        credential = _create_authorization(
            dashboard=dashboard,
            revision=revision,
            viewer=viewer,
            audience=RenderAuthorization.Audience.PREVIEW,
            issued_at=issued_at,
        )
        AuditEvent.objects.create(
            event_type="dashboard.preview_started",
            actor=viewer,
            dashboard=dashboard,
            revision=revision,
            metadata={"audience": RenderAuthorization.Audience.PREVIEW},
        )
        return credential


def issue_published_view(
    *,
    dashboard_id: UUID,
    viewer_id: UUID,
    now: datetime | None = None,
) -> RenderCredential:
    """Issue a stable-URL view for an owner or active grant to the pinned revision."""
    issued_at = now or timezone.now()
    with transaction.atomic(durable=True):
        dashboard = (
            Dashboard.objects.select_for_update()
            .select_related("published_revision")
            .filter(
                id=dashboard_id,
                state=Dashboard.State.PUBLISHED,
                published_revision__isnull=False,
            )
            .first()
        )
        viewer = User.objects.select_for_update().filter(id=viewer_id, is_active=True).first()
        if dashboard is None or viewer is None or dashboard.published_revision is None:
            raise RenderAuthorizationDenied("render authorization is not available")
        if (
            dashboard.owner_id != viewer.id
            and not ViewerGrant.objects.filter(
                dashboard=dashboard,
                viewer=viewer,
                revoked_at__isnull=True,
            ).exists()
        ):
            raise RenderAuthorizationDenied("render authorization is not available")
        credential = _create_authorization(
            dashboard=dashboard,
            revision=dashboard.published_revision,
            viewer=viewer,
            audience=RenderAuthorization.Audience.VIEWER,
            issued_at=issued_at,
        )
        AuditEvent.objects.create(
            event_type="dashboard.view_started",
            actor=viewer,
            dashboard=dashboard,
            revision=dashboard.published_revision,
            metadata={"audience": RenderAuthorization.Audience.VIEWER},
        )
        return credential


def resolve_render_authorization(
    token: str,
    *,
    audience: str,
    now: datetime | None = None,
) -> AuthorizedRender:
    """Resolve and re-authorize every HTML or CSV request from current database state."""
    checked_at = now or timezone.now()
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise RenderAuthorizationDenied("render authorization is not available")
    authorization = (
        RenderAuthorization.objects.select_related("viewer", "dashboard", "revision")
        .filter(token_digest=_digest(token), audience=audience)
        .first()
    )
    if authorization is None or not _authorization_is_current(authorization, checked_at):
        raise RenderAuthorizationDenied("render authorization is not available")
    return AuthorizedRender(
        authorization=authorization,
        dashboard=authorization.dashboard,
        revision=authorization.revision,
        viewer=authorization.viewer,
    )


def revoke_render_authorization(
    authorization_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    """End a credential early; repeated revocation is idempotent."""
    revoked_at = now or timezone.now()
    with transaction.atomic(durable=True):
        authorization = (
            RenderAuthorization.objects.select_for_update().filter(id=authorization_id).first()
        )
        if authorization is None or authorization.revoked_at is not None:
            return
        authorization.revoked_at = revoked_at
        authorization.save(update_fields=("revoked_at",))


def _create_authorization(
    *,
    dashboard: Dashboard,
    revision: Revision,
    viewer: User,
    audience: str,
    issued_at: datetime,
) -> RenderCredential:
    expires_at = issued_at + timedelta(seconds=settings.AGORA_RENDER_AUTH_TTL_SECONDS)
    for _ in range(_TOKEN_ATTEMPTS):
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        try:
            with transaction.atomic():
                RenderAuthorization.objects.create(
                    token_digest=_digest(token),
                    audience=audience,
                    viewer=viewer,
                    viewer_auth_version=viewer.auth_version,
                    dashboard=dashboard,
                    revision=revision,
                    expires_at=expires_at,
                )
        except IntegrityError:
            continue
        return RenderCredential(token=token, audience=audience, expires_at=expires_at)
    raise RenderAuthorizationUnavailable("render authorization could not be issued")


def _authorization_is_current(
    authorization: RenderAuthorization,
    checked_at: datetime,
) -> bool:
    if (
        authorization.revoked_at is not None
        or authorization.expires_at <= checked_at
        or not authorization.viewer.is_active
        or authorization.viewer.auth_version != authorization.viewer_auth_version
        or not authorization.revision.artifacts_locked
        or authorization.revision.dashboard_id != authorization.dashboard_id
    ):
        return False
    if authorization.audience == RenderAuthorization.Audience.PREVIEW:
        return (
            authorization.dashboard.owner_id == authorization.viewer_id
            and authorization.dashboard.state
            not in {Dashboard.State.ARCHIVED, Dashboard.State.DELETED}
        )
    if authorization.audience != RenderAuthorization.Audience.VIEWER:
        return False
    if (
        authorization.dashboard.state != Dashboard.State.PUBLISHED
        or authorization.dashboard.published_revision_id != authorization.revision_id
    ):
        return False
    return (
        authorization.dashboard.owner_id == authorization.viewer_id
        or ViewerGrant.objects.filter(
            dashboard_id=authorization.dashboard_id,
            viewer_id=authorization.viewer_id,
            revoked_at__isnull=True,
        ).exists()
    )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


__all__ = [
    "AuthorizedRender",
    "RenderAuthorizationDenied",
    "RenderAuthorizationUnavailable",
    "RenderCredential",
    "issue_owner_preview",
    "issue_published_view",
    "resolve_render_authorization",
    "revoke_render_authorization",
]
