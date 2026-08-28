"""Physical Oracle table names owned by the Agora deployment."""

from __future__ import annotations

from threading import Lock
from typing import Any, Final

from django.apps import apps
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Model

PROJECT_TABLE_PREFIX: Final = "TB_TA_AGORA_"

DJANGO_TABLE_RENAMES: Final[dict[str, str]] = {
    "auth_group": f"{PROJECT_TABLE_PREFIX}AUTH_GROUP",
    "auth_group_permissions": f"{PROJECT_TABLE_PREFIX}AUTH_GROUP_PERMISSIONS",
    "auth_permission": f"{PROJECT_TABLE_PREFIX}AUTH_PERMISSION",
    "django_content_type": f"{PROJECT_TABLE_PREFIX}DJANGO_CONTENT_TYPE",
    "django_migrations": f"{PROJECT_TABLE_PREFIX}DJANGO_MIGRATIONS",
    "django_session": f"{PROJECT_TABLE_PREFIX}DJANGO_SESSION",
}

_recorder_selection_lock = Lock()
_recorder_table_selected = False


def _migration_recorder_table_is_selected() -> bool:
    return _recorder_table_selected


def _set_model_table(
    model: type[Model],
    table_name: str,
    *,
    explicit: bool = True,
) -> None:
    model._meta.db_table = table_name
    if explicit:
        model._meta.original_attrs["db_table"] = table_name
    else:
        model._meta.original_attrs.pop("db_table", None)
    for field in model._meta.local_fields:
        field.__dict__.pop("cached_col", None)


def configure_django_runtime_table_names(*, prefixed: bool) -> None:
    """Point installed framework models at either side of the reversible rename."""

    def table_name(original: str) -> str:
        return DJANGO_TABLE_RENAMES[original] if prefixed else original

    if apps.is_installed("django.contrib.contenttypes"):
        from django.contrib.contenttypes.models import ContentType

        _set_model_table(ContentType, table_name("django_content_type"))

    if apps.is_installed("django.contrib.auth"):
        from django.contrib.auth.models import Group, Permission

        _set_model_table(Group, table_name("auth_group"), explicit=prefixed)
        _set_model_table(Group.permissions.through, table_name("auth_group_permissions"))
        _set_model_table(Permission, table_name("auth_permission"), explicit=prefixed)

    if apps.is_installed("django.contrib.sessions"):
        from django.contrib.sessions.models import Session

        _set_model_table(Session, table_name("django_session"))


def configure_migration_recorder_table(*, prefixed: bool) -> None:
    """Update Django's process-local migration-recorder model."""

    global _recorder_table_selected
    _set_model_table(
        MigrationRecorder.Migration,
        (DJANGO_TABLE_RENAMES["django_migrations"].lower() if prefixed else "django_migrations"),
    )
    _recorder_table_selected = True


def select_migration_recorder_table(raw_connection: Any) -> None:
    """Select the recorder table that physically exists when a process first connects."""

    global _recorder_table_selected
    if _migration_recorder_table_is_selected():
        return
    with _recorder_selection_lock:
        if _migration_recorder_table_is_selected():
            return
        prefixed_name = DJANGO_TABLE_RENAMES["django_migrations"]
        with raw_connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = :table_name",
                {"table_name": prefixed_name},
            )
            prefixed = cursor.fetchone()[0] == 1
        configure_migration_recorder_table(prefixed=prefixed)
