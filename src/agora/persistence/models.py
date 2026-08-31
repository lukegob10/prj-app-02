"""Oracle-backed Agora metadata models."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.crypto import salted_hmac

from agora.persistence.names import (
    InvalidDashboardTag,
    InvalidLogicalName,
    InvalidSoeid,
    canonicalize_soeid,
    normalize_dashboard_tag,
    normalize_logical_name,
)

STORAGE_KEY_PATTERN = r"^v1/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

# Keep the MIME contract in one place so model validation, persistence services, and the
# database check constraint cannot silently drift apart.  IMAGE and FONT deliberately have
# more than one valid subtype; callers must carry the authoritative subtype from upload
# validation rather than having the persistence layer guess from the broad artifact kind.
ARTIFACT_MEDIA_TYPES: dict[str, tuple[str, ...]] = {
    "html": ("text/html",),
    "csv": ("text/csv",),
    "css": ("text/css",),
    "img": ("image/png", "image/jpeg", "image/gif", "image/webp"),
    "font": ("font/woff", "font/woff2"),
}


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
        db_table = "TB_TA_AGORA_USER"
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
        db_table = "TB_TA_AGORA_LOGIN_THROTTLE"
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
    publication_version = models.PositiveBigIntegerField(default=0, editable=False)
    last_published_at = models.DateTimeField(null=True, blank=True, editable=False)
    publication_note = models.CharField(max_length=240, blank=True)
    data_as_of = models.DateTimeField(null=True, blank=True)
    freshness_interval_seconds = models.PositiveBigIntegerField(null=True, blank=True)
    freshness_confirmed_at = models.DateTimeField(null=True, blank=True)
    stale_after = models.DateTimeField(null=True, blank=True, editable=False)
    last_ownership_transfer = models.OneToOneField(
        "DashboardOwnershipTransfer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="current_for_dashboard",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "TB_TA_AGORA_DASHBOARD"
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
            models.CheckConstraint(
                condition=(
                    models.Q(publication_version=0, last_published_at__isnull=True)
                    | models.Q(publication_version__gt=0, last_published_at__isnull=False)
                ),
                name="agora_dash_pub_version_time",
            ),
            models.CheckConstraint(
                condition=(~models.Q(state="published") | models.Q(publication_version__gt=0)),
                name="agora_dash_published_versioned",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        freshness_interval_seconds__isnull=True,
                        freshness_confirmed_at__isnull=True,
                        stale_after__isnull=True,
                    )
                    | models.Q(
                        freshness_interval_seconds__isnull=False,
                        freshness_interval_seconds__gt=0,
                        freshness_interval_seconds__lte=31_536_000,
                        freshness_confirmed_at__isnull=False,
                        stale_after__isnull=False,
                    )
                ),
                name="agora_dash_fresh_fields_match",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("owner", "state"), name="agora_dash_owner_state_idx"),
            models.Index(fields=("state", "updated_at"), name="agora_dash_state_updated_idx"),
            models.Index(fields=("owner", "stale_after", "id"), name="agora_dash_owner_stale_idx"),
            models.Index(fields=("stale_after", "id"), name="agora_dash_stale_scan_idx"),
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
                or self.publication_version != 0
                or self.last_published_at is not None
                or self.last_ownership_transfer_id is not None
            ):
                raise ValidationError("new dashboards must begin as private drafts")
        else:
            original = type(self).objects.get(pk=self.pk)
            if (
                original.id != self.id
                or original.owner_id != self.owner_id
                or original.last_ownership_transfer_id != self.last_ownership_transfer_id
            ):
                raise ImmutableRecordError("dashboard identity and ownership are immutable")
            if (
                self.state == self.State.PUBLISHED
                and self.published_revision_id is not None
                and (
                    original.state != self.State.PUBLISHED
                    or self.published_revision_id != original.published_revision_id
                )
                and self.publication_version == original.publication_version
            ):
                self.publication_version = original.publication_version + 1
                self.last_published_at = timezone.now()
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = tuple(
                        dict.fromkeys((*update_fields, "publication_version", "last_published_at"))
                    )
            self._validate_lifecycle_transition(original)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("dashboards are soft-deleted through lifecycle state")

    def clean(self) -> None:
        super().clean()
        freshness_fields = (
            self.freshness_interval_seconds,
            self.freshness_confirmed_at,
            self.stale_after,
        )
        if any(value is None for value in freshness_fields) and any(
            value is not None for value in freshness_fields
        ):
            raise ValidationError(
                "freshness interval, confirmation, and stale-after must be set together"
            )
        if self.freshness_interval_seconds is not None:
            if not 1 <= self.freshness_interval_seconds <= 31_536_000:
                raise ValidationError(
                    {"freshness_interval_seconds": "interval must be between 1 second and 1 year"}
                )
            assert self.freshness_confirmed_at is not None
            expected_stale_after = self.freshness_confirmed_at + timedelta(
                seconds=self.freshness_interval_seconds
            )
            if self.stale_after != expected_stale_after:
                raise ValidationError(
                    {"stale_after": "stale-after must equal confirmation plus interval"}
                )
        if (self.publication_version == 0) != (self.last_published_at is None):
            raise ValidationError(
                "publication version zero and missing last-published time must match"
            )
        if self.state == self.State.PUBLISHED and self.publication_version == 0:
            raise ValidationError("published dashboards must have a publication version")
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

        if self.publication_version < original.publication_version:
            raise ImmutableRecordError("publication version cannot decrease")
        if self.publication_version > original.publication_version + 1:
            raise ValidationError("publication version must advance one version at a time")
        publication_advanced = self.publication_version == original.publication_version + 1
        if publication_advanced:
            if self.state != self.State.PUBLISHED or self.published_revision_id is None:
                raise ValidationError("a publication version requires a published revision")
            if self.last_published_at is None or (
                original.last_published_at is not None
                and self.last_published_at < original.last_published_at
            ):
                raise ValidationError("last-published time must advance with publication version")
        else:
            if self.last_published_at != original.last_published_at:
                raise ImmutableRecordError("last-published time changes only on publication")
            if self.publication_note != original.publication_note:
                raise ImmutableRecordError("publication note changes only on publication")
            if (
                self.state == self.State.PUBLISHED
                and original.state == self.State.PUBLISHED
                and self.published_revision_id != original.published_revision_id
            ):
                raise ValidationError("republishing a revision must advance publication version")

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
            stable_fields = (
                "name",
                "description",
                "latest_revision_id",
                "publication_note",
                "data_as_of",
                "freshness_interval_seconds",
                "freshness_confirmed_at",
                "stale_after",
            )
            if any(getattr(self, field) != getattr(original, field) for field in stable_fields):
                raise ValidationError("archived dashboards are read-only")


class DashboardOwnershipTransfer(models.Model):
    """Append-only, chained authorization marker for one ownership transfer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="ownership_transfers",
    )
    from_owner = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="outgoing_dashboard_transfers",
    )
    to_owner = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="incoming_dashboard_transfers",
    )
    previous_transfer = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="next_transfer",
    )
    transferred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "TB_TA_AGORA_DASH_TRANSFER"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~models.Q(from_owner=models.F("to_owner")),
                name="agora_transfer_owners_differ",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("dashboard", "-transferred_at", "id"),
                name="agora_transfer_history_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("ownership transfer history is immutable")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("ownership transfer history is retained")

    def clean(self) -> None:
        super().clean()
        if not self._state.adding or self.dashboard_id is None:
            return
        if self.from_owner_id != self.dashboard.owner_id:
            raise ValidationError({"from_owner": "source must be the current dashboard owner"})
        if self.to_owner_id == self.from_owner_id:
            raise ValidationError({"to_owner": "incoming owner must be different"})
        if self.to_owner_id is not None and not self.to_owner.is_active:
            raise ValidationError({"to_owner": "incoming owner must be active"})
        if self.previous_transfer_id != self.dashboard.last_ownership_transfer_id:
            raise ValidationError(
                {"previous_transfer": "transfer marker must continue the dashboard chain"}
            )


class DashboardTag(models.Model):
    """One of at most five effective, canonically keyed dashboard tags."""

    id = models.BigAutoField(primary_key=True)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="tags")
    label = models.CharField(max_length=40)
    key = models.CharField(max_length=80)
    slot = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "TB_TA_AGORA_DASHBOARD_TAG"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(slot__gte=1, slot__lte=5),
                name="agora_tag_slot_range",
            ),
            models.CheckConstraint(
                condition=~models.Q(label="") & ~models.Q(key=""),
                name="agora_tag_values_not_empty",
            ),
            models.UniqueConstraint(
                fields=("dashboard", "key"),
                name="agora_tag_dashboard_key_uq",
            ),
            models.UniqueConstraint(
                fields=("dashboard", "slot"),
                name="agora_tag_dashboard_slot_uq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("key", "dashboard"), name="agora_tag_lookup_idx")
        ]
        ordering = ("slot", "id")

    def __str__(self) -> str:
        return self.label

    def save(self, *args: Any, **kwargs: Any) -> None:
        normalized = normalize_dashboard_tag(self.label)
        self.label = normalized.display
        self.key = normalized.key
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        try:
            normalized = normalize_dashboard_tag(self.label)
        except InvalidDashboardTag as error:
            raise ValidationError({"label": str(error)}) from error
        if self.label != normalized.display or self.key != normalized.key:
            raise ValidationError("dashboard tag label and key must be canonical")


class DashboardFavorite(models.Model):
    """One idempotent favorite marker for an exact user/dashboard pair."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dashboard_favorites")
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="favorites")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "TB_TA_AGORA_DASH_FAVORITE"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("user", "dashboard"),
                name="agora_favorite_user_dash_uq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("user", "-created_at", "-id"),
                name="agora_favorite_recent_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.dashboard_id}"


class DashboardViewerState(models.Model):
    """Compact per-user state for recent views and publication-version badges."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dashboard_view_states")
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="viewer_states")
    last_viewed_at = models.DateTimeField()
    seen_publication_version = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "TB_TA_AGORA_DASH_VIEWER_STATE"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("user", "dashboard"),
                name="agora_view_state_user_dash_uq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("user", "-last_viewed_at", "-id"),
                name="agora_view_state_recent_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.dashboard_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if original.user_id != self.user_id or original.dashboard_id != self.dashboard_id:
                raise ImmutableRecordError("viewer-state scope is immutable")
            if self.last_viewed_at < original.last_viewed_at:
                raise ImmutableRecordError("last-viewed time cannot move backward")
            if self.seen_publication_version < original.seen_publication_version:
                raise ImmutableRecordError("seen publication version cannot decrease")
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if (
            self.dashboard_id is not None
            and self.seen_publication_version > self.dashboard.publication_version
        ):
            raise ValidationError(
                {"seen_publication_version": "seen version cannot exceed dashboard version"}
            )


class AccessRequest(models.Model):
    """One reusable, deduplicated access-request lifecycle per requester/dashboard."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"
        CANCELLED = "cancelled", "Cancelled"

    id = models.BigAutoField(primary_key=True)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="access_requests",
    )
    requester = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="dashboard_access_requests",
    )
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    message = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_dashboard_access_requests",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "TB_TA_AGORA_ACCESS_REQUEST"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "approved", "denied", "cancelled"]),
                name="agora_access_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="pending",
                        resolved_at__isnull=True,
                        resolved_by__isnull=True,
                    )
                    | (
                        ~models.Q(status="pending")
                        & models.Q(resolved_at__isnull=False, resolved_by__isnull=False)
                    )
                ),
                name="agora_access_resolution_match",
            ),
            models.UniqueConstraint(
                fields=("dashboard", "requester"),
                name="agora_access_dash_requester_uq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("dashboard", "status", "-requested_at", "-id"),
                name="agora_access_owner_queue_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.requester_id}:{self.status}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding:
            if self.status != self.Status.PENDING:
                raise ValidationError("new access requests must begin pending")
            if self.dashboard_id is not None and self.requester_id == self.dashboard.owner_id:
                raise ValidationError({"requester": "dashboard owner already has access"})
            if self.dashboard_id is not None and (
                self.dashboard.state != Dashboard.State.PUBLISHED
                or not self.dashboard.owner.is_active
            ):
                raise ValidationError({"dashboard": "dashboard is not accepting access requests"})
            if self.requester_id is not None and not self.requester.is_active:
                raise ValidationError({"requester": "requester must be active"})
        else:
            original = type(self).objects.get(pk=self.pk)
            self._validate_transition(original)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("access-request rows are retained and reused")

    def clean(self) -> None:
        super().clean()
        pending = self.status == self.Status.PENDING
        if pending != (self.resolved_at is None and self.resolved_by_id is None):
            raise ValidationError("pending and resolution fields do not match")

    def _validate_transition(self, original: AccessRequest) -> None:
        if original.dashboard_id != self.dashboard_id or original.requester_id != self.requester_id:
            raise ImmutableRecordError("access-request scope is immutable")
        if original.status == self.Status.PENDING:
            if self.status == self.Status.PENDING:
                if self.requested_at != original.requested_at:
                    raise ImmutableRecordError("pending request time is immutable")
                return
            if self.status not in {
                self.Status.APPROVED,
                self.Status.DENIED,
                self.Status.CANCELLED,
            }:
                raise ValidationError("access-request resolution is not valid")
            expected_resolver = (
                self.requester_id
                if self.status == self.Status.CANCELLED
                else self.dashboard.owner_id
            )
            if self.resolved_by_id != expected_resolver or self.resolved_at is None:
                raise ValidationError("access-request resolver is not authorized")
            if self.status == self.Status.APPROVED and (
                self.dashboard.state not in {Dashboard.State.PUBLISHED, Dashboard.State.UNPUBLISHED}
                or not self.dashboard.owner.is_active
                or not self.requester.is_active
            ):
                raise ValidationError("dashboard access cannot be approved in the current state")
            if self.requested_at != original.requested_at or self.message != original.message:
                raise ImmutableRecordError("resolution cannot rewrite request history")
            return
        if self.status == self.Status.PENDING:
            if self.requested_at <= original.requested_at:
                raise ValidationError("a reopened request must have a later request time")
            if self.resolved_at is not None or self.resolved_by_id is not None:
                raise ValidationError("a reopened request must clear its prior resolution")
            if self.requester_id is not None and not self.requester.is_active:
                raise ValidationError({"requester": "requester must be active"})
            if self.dashboard_id is not None and self.requester_id == self.dashboard.owner_id:
                raise ValidationError({"requester": "dashboard owner already has access"})
            if self.dashboard_id is not None and (
                self.dashboard.state != Dashboard.State.PUBLISHED
                or not self.dashboard.owner.is_active
            ):
                raise ValidationError({"dashboard": "dashboard is not accepting access requests"})
            return
        stable_fields = (
            "status",
            "message",
            "requested_at",
            "resolved_at",
            "resolved_by_id",
        )
        if any(getattr(self, field) != getattr(original, field) for field in stable_fields):
            raise ImmutableRecordError("resolved access request is immutable until reopened")


class Revision(models.Model):
    """One complete, immutable dashboard revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_revisions")
    created_at = models.DateTimeField(auto_now_add=True)
    artifacts_locked = models.BooleanField(default=False, editable=False)

    class Meta:
        db_table = "TB_TA_AGORA_REVISION"
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
        if self._state.adding and self.dashboard_id is not None:
            if self.created_by_id != self.dashboard.owner_id:
                raise ValidationError({"created_by": "revision creator must own the dashboard"})
            if self.dashboard.state not in {
                Dashboard.State.DRAFT,
                Dashboard.State.PUBLISHED,
                Dashboard.State.UNPUBLISHED,
            }:
                raise ValidationError({"dashboard": "dashboard state does not accept revisions"})
        if self._state.adding and self.created_by_id is not None and not self.created_by.is_active:
            raise ValidationError({"created_by": "revision creator must be active"})


class Artifact(models.Model):
    """Immutable metadata for one privately stored dashboard artifact."""

    class Kind(models.TextChoices):
        HTML = "html", "HTML"
        CSV = "csv", "CSV"
        CSS = "css", "CSS"
        IMAGE = "img", "Image"
        FONT = "font", "Font"

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
        db_table = "TB_TA_AGORA_ARTIFACT"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(kind__in=list(ARTIFACT_MEDIA_TYPES)),
                name="agora_artifact_valid_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="html", media_type="text/html")
                    | models.Q(kind="csv", media_type="text/csv")
                    | models.Q(kind="css", media_type="text/css")
                    | models.Q(kind="img", media_type__in=ARTIFACT_MEDIA_TYPES["img"])
                    | models.Q(kind="font", media_type__in=ARTIFACT_MEDIA_TYPES["font"])
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
        allowed_media_types = ARTIFACT_MEDIA_TYPES.get(self.kind)
        if allowed_media_types is None or self.media_type not in allowed_media_types:
            raise ValidationError(
                {"media_type": "media type is not allowed for this artifact kind"}
            )
        if self.revision_id is not None and self.revision.artifacts_locked:
            raise ValidationError("artifacts cannot be added to a complete revision")


class ViewerGrant(models.Model):
    """One retained, immutable dashboard-to-viewer grant epoch.

    A dashboard/viewer pair may have many retained epochs over its lifetime, but
    at most one epoch can be active.  The active-only invariant is installed by
    the Oracle-native migration because Django's conditional unique constraints
    do not express the function-based index used by this project.
    """

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
        db_table = "TB_TA_AGORA_VIEWER_GRANT"
        constraints: ClassVar[list[models.BaseConstraint]] = [
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
            models.Index(
                fields=("viewer", "revoked_at", "-created_at", "-id"),
                name="agora_grant_viewer_active_idx",
            ),
            models.Index(fields=("dashboard", "revoked_at"), name="agora_grant_dash_active_idx"),
            models.Index(
                fields=("dashboard", "viewer", "revoked_at"),
                name="agora_grant_scope_active_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.viewer_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = ("id", "dashboard_id", "viewer_id", "created_by_id", "created_at")
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ImmutableRecordError("grant relationship fields are immutable")
            if original.revoked_at is not None and (
                original.revoked_at != self.revoked_at
                or original.revoked_by_id != self.revoked_by_id
            ):
                raise ImmutableRecordError("a recorded grant revocation is immutable")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("viewer grants are retained and cannot be deleted")

    def clean(self) -> None:
        super().clean()
        if self._state.adding:
            if self.viewer_id == self.dashboard.owner_id:
                raise ValidationError({"viewer": "dashboard owners cannot grant themselves access"})
            if self.created_by_id != self.dashboard.owner_id:
                raise ValidationError({"created_by": "only the dashboard owner can create a grant"})
        if (self.revoked_at is None) != (self.revoked_by_id is None):
            raise ValidationError(
                "revoked_at and revoked_by must either both be set or both be empty"
            )
        if self.revoked_by_id is not None:
            is_new_revocation = self._state.adding
            if not self._state.adding:
                original = type(self).objects.only("revoked_at").get(pk=self.pk)
                is_new_revocation = original.revoked_at is None
            if is_new_revocation and self.revoked_by_id != self.dashboard.owner_id:
                raise ValidationError({"revoked_by": "only the dashboard owner can revoke a grant"})


class RenderAuthorization(models.Model):
    """Short-lived, hashed bearer authorization for the isolated content origin."""

    class Audience(models.TextChoices):
        PREVIEW = "preview", "Owner preview"
        VIEWER = "viewer", "Published viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    audience = models.CharField(max_length=8, choices=Audience)
    viewer = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="render_authorizations",
    )
    viewer_auth_version = models.PositiveBigIntegerField(editable=False)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="render_authorizations",
    )
    revision = models.ForeignKey(
        Revision,
        on_delete=models.PROTECT,
        related_name="render_authorizations",
    )
    viewer_grant = models.ForeignKey(
        ViewerGrant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="render_authorizations",
    )
    owner_transfer_epoch = models.ForeignKey(
        DashboardOwnershipTransfer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="render_authorizations",
    )
    publication_version = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    authorized_open_captured_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        db_table = "TB_TA_AGORA_RENDER_AUTHORIZATION"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(token_digest__regex=SHA256_PATTERN),
                name="agora_render_token_digest_format",
            ),
            models.CheckConstraint(
                condition=models.Q(audience__in=["preview", "viewer"]),
                name="agora_render_audience_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(viewer_auth_version__gt=0),
                name="agora_render_auth_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(audience="preview", publication_version__isnull=True)
                    | models.Q(
                        audience="viewer",
                        publication_version__isnull=False,
                        publication_version__gt=0,
                    )
                ),
                name="agora_render_pub_version_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(owner_transfer_epoch__isnull=True)
                    | models.Q(viewer_grant__isnull=True)
                ),
                name="agora_render_epoch_grant_xor",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("expires_at",), name="agora_render_expiry_idx"),
            models.Index(
                fields=("dashboard", "viewer", "audience"),
                name="agora_render_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.audience}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = (
                "id",
                "token_digest",
                "audience",
                "viewer_id",
                "viewer_auth_version",
                "dashboard_id",
                "revision_id",
                "viewer_grant_id",
                "owner_transfer_epoch_id",
                "publication_version",
                "created_at",
                "expires_at",
            )
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ImmutableRecordError("render authorization scope is immutable")
            if original.revoked_at is not None and self.revoked_at != original.revoked_at:
                raise ImmutableRecordError("render authorization revocation is immutable")
            if (
                original.authorized_open_captured_at is not None
                and self.authorized_open_captured_at != original.authorized_open_captured_at
            ):
                raise ImmutableRecordError("authorized-open capture marker is immutable")
        self.full_clean(validate_unique=False)
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("render authorizations expire or are revoked")

    def clean(self) -> None:
        super().clean()
        if self.authorized_open_captured_at is not None and (
            self.audience != self.Audience.VIEWER
            or self.created_at is None
            or self.authorized_open_captured_at != self.created_at
        ):
            raise ValidationError(
                {"authorized_open_captured_at": "capture marker must match a viewer issue"}
            )
        if self.audience == self.Audience.PREVIEW and self.publication_version is not None:
            raise ValidationError({"publication_version": "preview has no publication version"})
        if self.audience == self.Audience.VIEWER and (
            self.publication_version is None or self.publication_version <= 0
        ):
            raise ValidationError({"publication_version": "viewer issue must snapshot its version"})
        if self.revision_id is not None and self.dashboard_id is not None:
            if self.revision.dashboard_id != self.dashboard_id:
                raise ValidationError({"revision": "revision must belong to the dashboard"})
        if self.owner_transfer_epoch_id is not None:
            epoch = self.owner_transfer_epoch
            if epoch is None or epoch.dashboard_id != self.dashboard_id:
                raise ValidationError(
                    {"owner_transfer_epoch": "owner epoch must belong to the dashboard"}
                )
            if self.viewer_grant_id is not None:
                raise ValidationError(
                    {"owner_transfer_epoch": "owner and grant epochs are mutually exclusive"}
                )
        if self.viewer_grant_id is not None:
            if self.audience != self.Audience.VIEWER:
                raise ValidationError(
                    {"viewer_grant": "grant-bound authorizations must target viewers"}
                )
            grant = self.viewer_grant
            if grant is None:
                raise ValidationError({"viewer_grant": "authorization grant does not exist"})
            if grant.dashboard_id != self.dashboard_id:
                raise ValidationError(
                    {"viewer_grant": "grant must belong to the authorization dashboard"}
                )
            if grant.viewer_id != self.viewer_id:
                raise ValidationError(
                    {"viewer_grant": "grant must belong to the authorization viewer"}
                )
            if self._state.adding and grant.revoked_at is not None:
                raise ValidationError({"viewer_grant": "authorization grant must be active"})
        elif (
            self._state.adding
            and self.audience == self.Audience.VIEWER
            and self.viewer_id is not None
            and self.dashboard_id is not None
            and self.viewer_id != self.dashboard.owner_id
        ):
            raise ValidationError(
                {"viewer_grant": "non-owner viewer authorizations must bind a grant epoch"}
            )
        if (
            self._state.adding
            and self.viewer_id is not None
            and self.dashboard_id is not None
            and self.viewer_id == self.dashboard.owner_id
            and self.owner_transfer_epoch_id != self.dashboard.last_ownership_transfer_id
        ):
            raise ValidationError(
                {"owner_transfer_epoch": "owner authorization must bind the current epoch"}
            )
        if (
            self._state.adding
            and self.viewer_id is not None
            and self.viewer_auth_version != self.viewer.auth_version
        ):
            raise ValidationError(
                {"viewer_auth_version": "authorization must bind the current user version"}
            )


class AuthorizedOpen(models.Model):
    """Retention-bounded raw source row for one successful published-view issue."""

    id = models.BigAutoField(primary_key=True)
    source_authorization = models.OneToOneField(
        RenderAuthorization,
        on_delete=models.PROTECT,
        related_name="authorized_open",
    )
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="authorized_open_events",
    )
    viewer = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="authorized_open_events",
    )
    revision = models.ForeignKey(
        Revision,
        on_delete=models.PROTECT,
        related_name="authorized_open_events",
    )
    publication_version = models.PositiveBigIntegerField()
    opened_at = models.DateTimeField()
    aggregated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "TB_TA_AGORA_AUTHORIZED_OPEN"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(publication_version__gt=0),
                name="agora_open_pub_version_pos",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("aggregated_at", "opened_at", "id"),
                name="agora_open_pending_idx",
            ),
            models.Index(fields=("opened_at", "id"), name="agora_open_retention_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.id}:{self.dashboard_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = (
                "id",
                "source_authorization_id",
                "dashboard_id",
                "viewer_id",
                "revision_id",
                "publication_version",
                "opened_at",
            )
            if any(getattr(self, field) != getattr(original, field) for field in immutable):
                raise ImmutableRecordError("authorized-open source fields are immutable")
            if original.aggregated_at is not None and self.aggregated_at != original.aggregated_at:
                raise ImmutableRecordError("authorized-open aggregation is one-way")
        self.full_clean(validate_unique=False)
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("authorized opens are removed only by bounded retention")

    def clean(self) -> None:
        super().clean()
        source = self.source_authorization
        if source.audience != RenderAuthorization.Audience.VIEWER:
            raise ValidationError({"source_authorization": "only published viewer issues count"})
        expected = (
            source.dashboard_id,
            source.viewer_id,
            source.revision_id,
            source.publication_version,
            source.created_at,
        )
        actual = (
            self.dashboard_id,
            self.viewer_id,
            self.revision_id,
            self.publication_version,
            self.opened_at,
        )
        if actual != expected:
            raise ValidationError("authorized-open snapshot must match its source authorization")


class DashboardOpenDaily(models.Model):
    """Asynchronously maintained daily authorized-open count per dashboard."""

    id = models.BigAutoField(primary_key=True)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="daily_open_rollups",
    )
    day = models.DateField()
    authorized_open_count = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "TB_TA_AGORA_OPEN_DAILY"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("dashboard", "day"),
                name="agora_open_daily_dash_day_uq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("dashboard", "-day", "-id"), name="agora_open_daily_read_idx")
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.day}"


class DashboardViewerOpenSummary(models.Model):
    """Asynchronously maintained authorized-open summary for one dashboard/viewer."""

    id = models.BigAutoField(primary_key=True)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="viewer_open_summaries",
    )
    viewer = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="dashboard_open_summaries",
    )
    authorized_open_count = models.PositiveBigIntegerField(default=0)
    first_opened_at = models.DateTimeField()
    last_opened_at = models.DateTimeField()

    class Meta:
        db_table = "TB_TA_AGORA_VIEWER_OPEN_SUM"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("dashboard", "viewer"),
                name="agora_view_sum_dash_viewer_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(last_opened_at__gte=models.F("first_opened_at")),
                name="agora_view_sum_time_order",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("dashboard", "-authorized_open_count", "viewer"),
                name="agora_view_sum_rank_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.dashboard_id}:{self.viewer_id}"


class DashboardOpenSnapshot(models.Model):
    """Asynchronously maintained dashboard total used by bounded popularity reads."""

    id = models.BigAutoField(primary_key=True)
    dashboard = models.OneToOneField(
        Dashboard,
        on_delete=models.PROTECT,
        related_name="open_snapshot",
    )
    authorized_open_count = models.PositiveBigIntegerField(default=0)
    last_opened_at = models.DateTimeField()
    captured_through_open_id = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "TB_TA_AGORA_OPEN_SNAPSHOT"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("-authorized_open_count", "dashboard"),
                name="agora_open_snapshot_rank_idx",
            )
        ]

    def __str__(self) -> str:
        return str(self.dashboard_id)


class AnalyticsPipelineCheckpoint(models.Model):
    """Single-row lock and progress marker for a named bounded analytics pipeline."""

    pipeline_key = models.CharField(max_length=32, primary_key=True)
    last_completed_open_id = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "TB_TA_AGORA_ANALYTICS_CKPT"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(pipeline_key="authorized_opens_v1"),
                name="agora_analytics_ckpt_key",
            )
        ]

    def __str__(self) -> str:
        return self.pipeline_key

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if self.pipeline_key != original.pipeline_key:
                raise ImmutableRecordError("analytics checkpoint key is immutable")
            if self.last_completed_open_id < original.last_completed_open_id:
                raise ImmutableRecordError("analytics checkpoint cannot move backward")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("analytics checkpoint is retained")


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
        db_table = "TB_TA_AGORA_AUDIT_EVENT"
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
        db_table = "TB_TA_AGORA_STORAGE_RESERVATION"
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
