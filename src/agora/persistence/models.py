"""PostgreSQL-backed Agora metadata models."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.crypto import salted_hmac

from agora.persistence.names import (
    InvalidLogicalName,
    InvalidSoeid,
    canonicalize_soeid,
    normalize_logical_name,
)

STORAGE_KEY_PATTERN = r"^v1/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ImmutableRecordError(ValidationError):
    """Raised when application code attempts to mutate retained history."""


class UserManager(BaseUserManager["User"]):
    """Create application identities only from canonical SOEIDs."""

    use_in_migrations = True

    def create_user(self, soeid: str, password: str | None = None, **extra: Any) -> User:
        if password is not None:
            raise ValueError("credential provisioning belongs to AG-003")
        user = self.model(soeid=canonicalize_soeid(soeid), **extra)
        user.set_unusable_password()
        user.full_clean()
        user.save(using=self._db, force_insert=True)
        return user

    def create_superuser(self, soeid: str, password: str | None = None, **extra: Any) -> User:
        raise ValueError("administrator credential provisioning belongs to AG-003")


class User(AbstractBaseUser):
    """Administrator-provisioned identity keyed by a canonical SOEID."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    soeid = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    is_administrator = models.BooleanField(default=False)
    auth_version = models.PositiveBigIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "soeid"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = UserManager()

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(soeid__regex=r"^[A-Z0-9][A-Z0-9._-]{0,63}$"),
                name="agora_user_canonical_soeid",
            ),
            models.CheckConstraint(
                condition=models.Q(auth_version__gt=0),
                name="agora_user_auth_version_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.soeid

    def get_session_auth_hash(self) -> str:
        """Bind every session to the current password and revocation version."""
        return salted_hmac(
            "agora.persistence.models.User.get_session_auth_hash",
            f"{self.password}:{self.auth_version}",
            secret=settings.SECRET_KEY,
        ).hexdigest()

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = (
                type(self).objects.only("id", "soeid", "is_active", "auth_version").get(pk=self.pk)
            )
            if original.id != self.id or original.soeid != self.soeid:
                raise ImmutableRecordError("user identity fields are immutable")
            if self.is_active != original.is_active:
                self.auth_version = original.auth_version + 1
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = tuple(dict.fromkeys((*update_fields, "auth_version")))
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("users are retained and cannot be hard-deleted")

    def clean(self) -> None:
        super().clean()
        try:
            canonical = canonicalize_soeid(self.soeid)
        except InvalidSoeid as error:
            raise ValidationError({"soeid": str(error)}) from error
        if self.soeid != canonical:
            raise ValidationError({"soeid": "SOEID must already be canonical"})


class LoginThrottle(models.Model):
    """Hashed, short-lived failure counters for bounded login abuse control."""

    id = models.BigAutoField(primary_key=True)
    bucket_hash = models.CharField(max_length=64, unique=True, editable=False)
    window_started_at = models.DateTimeField()
    failed_attempts = models.PositiveIntegerField(default=0)
    blocked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(bucket_hash__regex=r"^[0-9a-f]{64}$"),
                name="agora_login_throttle_hash_format",
            ),
            models.CheckConstraint(
                condition=models.Q(failed_attempts__gte=0),
                name="agora_login_throttle_attempts_nonnegative",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("updated_at",), name="agora_auth_throttle_upd_idx")
        ]

    def __str__(self) -> str:
        return f"login-throttle:{self.id}"


class Dashboard(models.Model):
    """Top-level securable resource with stable identity and publication pointers."""

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        UNPUBLISHED = "unpublished", "Unpublished"
        ARCHIVED = "archived", "Archived"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="owned_dashboards")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    state = models.CharField(max_length=16, choices=State, default=State.DRAFT)
    latest_revision = models.ForeignKey(
        "Revision",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="latest_for_dashboards",
    )
    published_revision = models.ForeignKey(
        "Revision",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="published_for_dashboards",
    )
    first_published_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "draft",
                        "published",
                        "unpublished",
                        "archived",
                        "deleted",
                    ]
                ),
                name="agora_dashboard_valid_state",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="published", published_revision__isnull=False)
                    | (~models.Q(state="published") & models.Q(published_revision__isnull=True))
                ),
                name="agora_dashboard_publication_state",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="draft", first_published_at__isnull=True)
                    | models.Q(
                        state__in=["published", "unpublished"],
                        first_published_at__isnull=False,
                    )
                    | models.Q(state__in=["archived", "deleted"])
                ),
                name="agora_dashboard_publication_history",
            ),
            models.CheckConstraint(
                condition=~models.Q(name=""),
                name="agora_dashboard_name_not_empty",
            ),
            models.UniqueConstraint(
                fields=("id", "owner"),
                name="agora_dashboard_id_owner_unique",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("owner", "state"), name="agora_dash_owner_state_idx"),
            models.Index(fields=("state", "updated_at"), name="agora_dash_state_updated_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding:
            if (
                self.state != self.State.DRAFT
                or self.latest_revision_id is not None
                or self.published_revision_id is not None
                or self.first_published_at is not None
            ):
                raise ValidationError("new dashboards must begin as private drafts")
        else:
            original = type(self).objects.get(pk=self.pk)
            if original.id != self.id or original.owner_id != self.owner_id:
                raise ImmutableRecordError("dashboard identity and ownership are immutable")
            self._validate_lifecycle_transition(original)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("dashboards are soft-deleted through lifecycle state")

    def clean(self) -> None:
        super().clean()
        if self.published_revision_id is not None:
            published_revision = self.published_revision
            if published_revision is None or published_revision.dashboard_id != self.id:
                raise ValidationError(
                    {"published_revision": "published revision must belong to this dashboard"}
                )
        if self.latest_revision_id is not None:
            latest_revision = self.latest_revision
            if latest_revision is None or latest_revision.dashboard_id != self.id:
                raise ValidationError(
                    {"latest_revision": "latest revision must belong to this dashboard"}
                )

    def _validate_lifecycle_transition(self, original: Dashboard) -> None:
        if original.state == self.State.DELETED:
            raise ImmutableRecordError("deleted dashboards are terminal tombstones")
        if original.first_published_at is not None:
            if self.first_published_at != original.first_published_at:
                raise ImmutableRecordError("first publication time is immutable")
        elif self.first_published_at is not None and not (
            original.state == self.State.DRAFT and self.state == self.State.PUBLISHED
        ):
            raise ValidationError("publication history begins only with first publication")

        allowed: dict[str, set[str]] = {
            self.State.DRAFT: {
                self.State.DRAFT,
                self.State.PUBLISHED,
                self.State.ARCHIVED,
                self.State.DELETED,
            },
            self.State.PUBLISHED: {
                self.State.PUBLISHED,
                self.State.UNPUBLISHED,
                self.State.ARCHIVED,
                self.State.DELETED,
            },
            self.State.UNPUBLISHED: {
                self.State.UNPUBLISHED,
                self.State.PUBLISHED,
                self.State.ARCHIVED,
                self.State.DELETED,
            },
            self.State.ARCHIVED: {
                self.State.UNPUBLISHED
                if original.first_published_at is not None
                else self.State.DRAFT,
                self.State.DELETED,
            },
        }
        if self.state not in allowed[original.state]:
            raise ValidationError("dashboard lifecycle transition is not allowed")
        if original.state == self.State.ARCHIVED:
            stable_fields = ("name", "description", "latest_revision_id")
            if any(getattr(self, field) != getattr(original, field) for field in stable_fields):
                raise ValidationError("archived dashboards are read-only")


class Revision(models.Model):
    """One complete, immutable dashboard revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_revisions")
    created_at = models.DateTimeField(auto_now_add=True)
    artifacts_locked = models.BooleanField(default=False, editable=False)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(number__gt=0),
                name="agora_revision_positive_number",
            ),
            models.UniqueConstraint(
                fields=("dashboard", "number"),
                name="agora_revision_dashboard_number_unique",
            ),
            models.UniqueConstraint(
                fields=("dashboard", "id"),
                name="agora_revision_dashboard_id_unique",
            ),
        ]
        ordering = ("dashboard_id", "number")

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.number}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("complete revisions are immutable")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("complete revisions are retained and cannot be deleted")

    def clean(self) -> None:
        super().clean()
        if self.dashboard_id is not None:
            if self.created_by_id != self.dashboard.owner_id:
                raise ValidationError({"created_by": "revision creator must own the dashboard"})
            if self.dashboard.state not in {
                Dashboard.State.DRAFT,
                Dashboard.State.PUBLISHED,
                Dashboard.State.UNPUBLISHED,
            }:
                raise ValidationError({"dashboard": "dashboard state does not accept revisions"})
        if self.created_by_id is not None and not self.created_by.is_active:
            raise ValidationError({"created_by": "revision creator must be active"})


class Artifact(models.Model):
    """Immutable metadata for one privately stored HTML or CSV artifact."""

    class Kind(models.TextChoices):
        HTML = "html", "HTML"
        CSV = "csv", "CSV"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(Revision, on_delete=models.PROTECT, related_name="artifacts")
    kind = models.CharField(max_length=4, choices=Kind)
    logical_name = models.CharField(max_length=255)
    name_key = models.CharField(max_length=1024, editable=False)
    storage_key = models.CharField(max_length=76, unique=True, editable=False)
    byte_size = models.PositiveBigIntegerField()
    media_type = models.CharField(max_length=32)
    sha256 = models.CharField(max_length=64)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(kind__in=["html", "csv"]),
                name="agora_artifact_valid_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="html", media_type="text/html")
                    | models.Q(kind="csv", media_type="text/csv")
                ),
                name="agora_artifact_kind_media_type",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gte=0),
                name="agora_artifact_nonnegative_size",
            ),
            models.CheckConstraint(
                condition=models.Q(sha256__regex=SHA256_PATTERN),
                name="agora_artifact_sha256_format",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_key__regex=STORAGE_KEY_PATTERN),
                name="agora_artifact_storage_key_format",
            ),
            models.CheckConstraint(
                condition=~models.Q(logical_name="") & ~models.Q(name_key=""),
                name="agora_artifact_names_not_empty",
            ),
            models.UniqueConstraint(
                fields=("revision", "name_key"),
                name="agora_artifact_revision_name_unique",
            ),
            models.UniqueConstraint(
                fields=("revision",),
                condition=models.Q(kind="html"),
                name="agora_artifact_one_html_per_revision",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("revision", "kind"), name="agora_art_rev_kind_idx")
        ]

    def __str__(self) -> str:
        return f"{self.revision_id}:{self.logical_name}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("artifact metadata is immutable")
        normalized = normalize_logical_name(self.logical_name)
        self.logical_name = normalized.display
        self.name_key = normalized.comparison_key
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("artifact metadata is retained and cannot be deleted")

    def clean(self) -> None:
        super().clean()
        try:
            normalized = normalize_logical_name(self.logical_name)
        except InvalidLogicalName as error:
            raise ValidationError({"logical_name": str(error)}) from error
        if self.logical_name != normalized.display or self.name_key != normalized.comparison_key:
            raise ValidationError("logical name and comparison key must be canonical")
        if self.revision_id is not None and self.revision.artifacts_locked:
            raise ValidationError("artifacts cannot be added to a complete revision")


class ViewerGrant(models.Model):
    """One retained, unique dashboard-to-viewer relationship."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.PROTECT, related_name="viewer_grants")
    viewer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="viewer_grants")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_grants")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="revoked_grants",
    )

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("dashboard", "viewer"),
                name="agora_grant_dashboard_viewer_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(revoked_at__isnull=True, revoked_by__isnull=True)
                    | models.Q(revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="agora_grant_revocation_fields_match",
            ),
            models.CheckConstraint(
                condition=~models.Q(viewer=models.F("created_by")),
                name="agora_grant_owner_not_viewer",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("viewer", "revoked_at"), name="agora_grant_viewer_active_idx"),
            models.Index(fields=("dashboard", "revoked_at"), name="agora_grant_dash_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.viewer_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = ("id", "dashboard_id", "viewer_id", "created_by_id", "created_at")
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ImmutableRecordError("grant relationship fields are immutable")
            if (
                original.revoked_at is not None
                and self.revoked_at is not None
                and (
                    original.revoked_at != self.revoked_at
                    or original.revoked_by_id != self.revoked_by_id
                )
            ):
                raise ImmutableRecordError("a recorded grant revocation is immutable")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("viewer grants are retained and cannot be deleted")

    def clean(self) -> None:
        super().clean()
        if self.viewer_id == self.dashboard.owner_id:
            raise ValidationError({"viewer": "dashboard owners cannot grant themselves access"})
        if self.created_by_id != self.dashboard.owner_id:
            raise ValidationError({"created_by": "only the dashboard owner can create a grant"})
        if self.revoked_by_id is not None and self.revoked_by_id != self.dashboard.owner_id:
            raise ValidationError({"revoked_by": "only the dashboard owner can revoke a grant"})


class AuditEvent(models.Model):
    """Append-only, content-free record of a security-relevant domain event."""

    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events"
    )
    dashboard = models.ForeignKey(
        Dashboard, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events"
    )
    revision = models.ForeignKey(
        Revision, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events"
    )
    target_user = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="targeted_audit_events"
    )
    request_id = models.UUIDField(null=True, blank=True)
    metadata = models.JSONField(blank=True, default=dict)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(event_type__regex=r"^[a-z][a-z0-9_.-]{2,63}$"),
                name="agora_audit_event_type_format",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("dashboard", "occurred_at", "id"), name="agora_audit_dash_time_idx"
            ),
            models.Index(fields=("actor", "occurred_at", "id"), name="agora_audit_actor_time_idx"),
            models.Index(fields=("event_type", "occurred_at"), name="agora_audit_type_time_idx"),
        ]
        ordering = ("occurred_at", "id")

    def __str__(self) -> str:
        return f"{self.id}:{self.event_type}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("audit events are append-only")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("audit events are append-only")

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.metadata, dict):
            raise ValidationError({"metadata": "audit metadata must be a JSON object"})


class StorageReservation(models.Model):
    """Durable cleanup ownership for a file not yet committed to an Artifact."""

    class StorageState(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        OWNED = "owned", "Owned bytes"
        COLLISION = "collision", "Collision; preserve bytes"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_key = models.CharField(max_length=76, unique=True, editable=False)
    storage_state = models.CharField(
        max_length=10,
        choices=StorageState,
        default=StorageState.RESERVED,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified_size = models.PositiveBigIntegerField(null=True, blank=True)
    verified_sha256 = models.CharField(max_length=64, blank=True, default="")
    cleanup_required = models.BooleanField(default=False)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(storage_key__regex=STORAGE_KEY_PATTERN),
                name="agora_reservation_storage_key_format",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_state__in=["reserved", "owned", "collision"]),
                name="agora_reservation_valid_storage_state",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(verified_size__isnull=True, verified_sha256="")
                    | (models.Q(verified_size__isnull=False) & ~models.Q(verified_sha256=""))
                ),
                name="agora_reservation_verification_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(verified_sha256="") | models.Q(verified_sha256__regex=SHA256_PATTERN)
                ),
                name="agora_reservation_sha256_format",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        storage_state__in=["reserved", "collision"],
                        verified_size__isnull=True,
                        verified_sha256="",
                    )
                    | (
                        models.Q(storage_state="owned", verified_size__isnull=False)
                        & ~models.Q(verified_sha256="")
                    )
                ),
                name="agora_reservation_state_receipt_match",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("cleanup_required", "expires_at"), name="agora_res_cleanup_expiry_idx"
            )
        ]

    def __str__(self) -> str:
        return str(self.id)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = ("id", "storage_key", "created_at", "expires_at")
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ImmutableRecordError("storage reservation identity is immutable")
            if original.verified_size is not None and (
                self.verified_size != original.verified_size
                or self.verified_sha256 != original.verified_sha256
            ):
                raise ImmutableRecordError("storage verification receipt is immutable")
            if self.storage_state != original.storage_state and not (
                original.storage_state == self.StorageState.RESERVED
                and self.storage_state in {self.StorageState.OWNED, self.StorageState.COLLISION}
            ):
                raise ImmutableRecordError("storage reservation state transition is not allowed")
            if original.cleanup_required and not self.cleanup_required:
                raise ImmutableRecordError("storage cleanup requirement cannot be cleared")
        self.full_clean(validate_unique=False)
        super().save(*args, **kwargs)
