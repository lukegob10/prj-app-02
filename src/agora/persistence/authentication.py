"""Transactional identity services for the local SOEID boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from django.conf import settings
from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import salted_hmac

from agora.persistence.models import AuditEvent, LoginThrottle, User
from agora.persistence.names import InvalidSoeid, canonicalize_soeid
from agora.persistence.querying import get_one_or_none

MAX_PASSWORD_LENGTH: Final = 256
AUTH_FAILURE_LIMIT: Final = 5
AUTH_FAILURE_WINDOW: Final = timedelta(minutes=15)
AUTH_LOCKOUT_DURATION: Final = timedelta(minutes=1)


class IdentityServiceError(RuntimeError):
    """Base class for expected identity-service policy failures."""


class BootstrapAlreadyComplete(IdentityServiceError):
    """Raised when first-administrator bootstrap is no longer available."""


class DuplicateSoeid(IdentityServiceError):
    """Raised when a canonical SOEID is already provisioned."""


class LastAdministratorError(IdentityServiceError):
    """Raised when an operation would remove the last active administrator."""


class NotAdministrator(IdentityServiceError):
    """Raised when a protected user-administration action lacks authority."""


class PasswordPolicyError(IdentityServiceError):
    """Raised when a password fails Django's configured validators."""

    def __init__(self, messages: tuple[str, ...]) -> None:
        self.messages = messages
        super().__init__(" ".join(messages))


class SelfDisableError(IdentityServiceError):
    """Raised when an administrator attempts to disable the current account."""


class UserNotFound(IdentityServiceError):
    """Raised when an administrator target no longer exists."""


def authenticate_login(request: HttpRequest, soeid: str, password: str) -> User | None:
    """Authenticate one form submission with generic failure and bounded throttling."""
    canonical = _try_canonicalize(soeid)
    supplied_password = password[:MAX_PASSWORD_LENGTH]
    known_user = (
        User.objects.only("id").filter(soeid=canonical).first() if canonical is not None else None
    )
    bucket_hashes = [_bucket_hash("ip", _remote_address(request))]
    if known_user is not None:
        bucket_hashes.append(_bucket_hash("user", str(known_user.id)))
    now = timezone.now()

    with transaction.atomic():
        buckets = _lock_throttle_buckets(bucket_hashes, now)
        if any(
            bucket.blocked_until is not None and bucket.blocked_until > now for bucket in buckets
        ):
            _dummy_password_work(supplied_password)
            return None

        user: User | None = None
        if canonical is not None and len(password) <= MAX_PASSWORD_LENGTH:
            user = django_authenticate(request, soeid=canonical, password=supplied_password)
        else:
            _dummy_password_work(supplied_password)

        if user is None:
            _register_failure(buckets, now)
            return None

        LoginThrottle.objects.filter(bucket_hash__in=bucket_hashes).delete()
        _record_audit_event("auth.login.succeeded", actor=user)
        return user


def bootstrap_first_administrator(soeid: str, password: str) -> User:
    """Create the only permitted unauthenticated administrator bootstrap account."""
    with transaction.atomic(durable=True):
        _acquire_bootstrap_lock()
        if User.objects.exists():
            raise BootstrapAlreadyComplete
        canonical = canonicalize_soeid(soeid)
        user = User(soeid=canonical, is_active=True, is_administrator=True)
        _set_validated_password(user, password)
        user.save(force_insert=True)
        _record_audit_event("auth.bootstrap", target_user=user)
        return user


def provision_user(
    *,
    actor_id: UUID,
    soeid: str,
    password: str,
    is_administrator: bool = False,
) -> User:
    """Provision one active user after rechecking administrator authority."""
    canonical = canonicalize_soeid(soeid)
    with transaction.atomic(durable=True):
        actor = _require_administrator(actor_id)
        user = User(
            soeid=canonical,
            is_active=True,
            is_administrator=is_administrator,
        )
        _set_validated_password(user, password)
        try:
            with transaction.atomic():
                user.save(force_insert=True)
        except (IntegrityError, ValidationError) as error:
            if User.objects.filter(soeid=canonical).exists():
                raise DuplicateSoeid from error
            raise
        _record_audit_event("user.created", actor=actor, target_user=user)
        return user


def disable_user(*, actor_id: UUID, target_id: UUID) -> User:
    """Disable one account while serializing the last-administrator check."""
    with transaction.atomic(durable=True):
        active_administrators = list(
            User.objects.select_for_update()
            .filter(is_active=True, is_administrator=True)
            .order_by("id")
        )
        actor = next((user for user in active_administrators if user.id == actor_id), None)
        if actor is None:
            raise NotAdministrator
        target = get_one_or_none(User.objects.select_for_update().filter(id=target_id))
        if target is None:
            raise UserNotFound
        if target.id == actor.id:
            if target.is_administrator and len(active_administrators) <= 1:
                raise LastAdministratorError
            raise SelfDisableError
        if not target.is_active:
            return target

        target.is_active = False
        target.save(update_fields=("is_active", "updated_at"))
        _record_audit_event("user.disabled", actor=actor, target_user=target)
        return target


def enable_user(*, actor_id: UUID, target_id: UUID) -> User:
    """Re-enable one retained account without changing its password."""
    with transaction.atomic(durable=True):
        actor = _require_administrator(actor_id)
        target = get_one_or_none(User.objects.select_for_update().filter(id=target_id))
        if target is None:
            raise UserNotFound
        if target.is_active:
            return target

        target.is_active = True
        target.save(update_fields=("is_active", "updated_at"))
        _record_audit_event("user.enabled", actor=actor, target_user=target)
        return target


def reset_user_password(*, actor_id: UUID, target_id: UUID, password: str) -> User:
    """Replace a target password and invalidate every previous session."""
    with transaction.atomic(durable=True):
        actor = _require_administrator(actor_id)
        target = get_one_or_none(User.objects.select_for_update().filter(id=target_id))
        if target is None:
            raise UserNotFound
        _set_validated_password(target, password)
        target.auth_version += 1
        target.save(update_fields=("password", "auth_version", "updated_at"))
        _record_audit_event("user.password_reset", actor=actor, target_user=target)
        return target


def record_logout(user: User) -> None:
    """Append a content-free logout event before the session is flushed."""
    _record_audit_event("auth.logout", actor=user)


def _try_canonicalize(value: str) -> str | None:
    try:
        return canonicalize_soeid(value)
    except InvalidSoeid:
        return None


def _set_validated_password(user: User, password: str) -> None:
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError((f"Password must be {MAX_PASSWORD_LENGTH} characters or fewer.",))
    try:
        validate_password(password, user=user)
    except ValidationError as error:
        raise PasswordPolicyError(tuple(error.messages)) from error
    user.set_password(password)


def _require_administrator(actor_id: UUID) -> User:
    actor = get_one_or_none(User.objects.select_for_update().filter(id=actor_id))
    if actor is None or not actor.is_active or not actor.is_administrator:
        raise NotAdministrator
    return actor


def _record_audit_event(
    event_type: str, *, actor: User | None = None, target_user: User | None = None
) -> None:
    """Write identity events through a fixed, metadata-free redaction boundary."""
    AuditEvent.objects.create(
        event_type=event_type,
        actor_id=actor.id if actor is not None else None,
        target_user_id=target_user.id if target_user is not None else None,
    )


def _acquire_bootstrap_lock() -> None:
    table = connection.ops.quote_name(User._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE")


def _remote_address(request: HttpRequest) -> str:
    return request.META.get("REMOTE_ADDR") or "unknown"


def _bucket_hash(scope: str, value: str) -> str:
    return salted_hmac(
        "agora.persistence.authentication.throttle",
        f"{scope}:{value}",
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()


def _lock_throttle_buckets(bucket_hashes: list[str], now: datetime) -> list[LoginThrottle]:
    current_time = now
    buckets: list[LoginThrottle] = []
    for bucket_hash in sorted(set(bucket_hashes)):
        bucket, _ = LoginThrottle.objects.get_or_create(
            bucket_hash=bucket_hash,
            defaults={"window_started_at": current_time},
        )
        bucket = LoginThrottle.objects.select_for_update().get(id=bucket.id)
        if current_time - bucket.window_started_at >= AUTH_FAILURE_WINDOW:
            bucket.window_started_at = current_time
            bucket.failed_attempts = 0
            bucket.blocked_until = None
            bucket.save(
                update_fields=(
                    "window_started_at",
                    "failed_attempts",
                    "blocked_until",
                    "updated_at",
                )
            )
        elif bucket.blocked_until is not None and bucket.blocked_until <= current_time:
            bucket.blocked_until = None
            bucket.save(update_fields=("blocked_until", "updated_at"))
        buckets.append(bucket)
    return buckets


def _register_failure(buckets: list[LoginThrottle], now: datetime) -> None:
    current_time = now
    for bucket in buckets:
        bucket.failed_attempts = min(bucket.failed_attempts + 1, AUTH_FAILURE_LIMIT)
        if bucket.failed_attempts >= AUTH_FAILURE_LIMIT:
            bucket.blocked_until = current_time + AUTH_LOCKOUT_DURATION
        bucket.save(update_fields=("failed_attempts", "blocked_until", "updated_at"))


def _dummy_password_work(password: str) -> None:
    User().set_password(password)
