"""Apply the Agora project prefix to domain and Django framework tables."""

from collections.abc import Mapping
from importlib import import_module
from typing import Protocol, cast

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation, OperationCategory
from django.db.migrations.state import ProjectState

from agora.db.table_names import (
    configure_django_runtime_table_names,
    configure_migration_recorder_table,
)

DOMAIN_TABLE_RENAMES = {
    "TB_TA_ARTIFACT": "TB_TA_AGORA_ARTIFACT",
    "TB_TA_AUDIT_EVENT": "TB_TA_AGORA_AUDIT_EVENT",
    "TB_TA_DASHBOARD": "TB_TA_AGORA_DASHBOARD",
    "TB_TA_LOGIN_THROTTLE": "TB_TA_AGORA_LOGIN_THROTTLE",
    "TB_TA_RENDER_AUTHORIZATION": "TB_TA_AGORA_RENDER_AUTHORIZATION",
    "TB_TA_REVISION": "TB_TA_AGORA_REVISION",
    "TB_TA_STORAGE_RESERVATION": "TB_TA_AGORA_STORAGE_RESERVATION",
    "TB_TA_USER": "TB_TA_AGORA_USER",
    "TB_TA_VIEWER_GRANT": "TB_TA_AGORA_VIEWER_GRANT",
}
FRAMEWORK_TABLE_RENAMES = {
    "AUTH_GROUP": "TB_TA_AGORA_AUTH_GROUP",
    "AUTH_GROUP_PERMISSIONS": "TB_TA_AGORA_AUTH_GROUP_PERMISSIONS",
    "AUTH_PERMISSION": "TB_TA_AGORA_AUTH_PERMISSION",
    "DJANGO_CONTENT_TYPE": "TB_TA_AGORA_DJANGO_CONTENT_TYPE",
    "DJANGO_SESSION": "TB_TA_AGORA_DJANGO_SESSION",
    # Keep the recorder last, then switch its process-local model immediately.
    "DJANGO_MIGRATIONS": "TB_TA_AGORA_DJANGO_MIGRATIONS",
}


class _TriggerMigration(Protocol):
    def _retarget_triggers(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        replacements: Mapping[str, str],
    ) -> None: ...


_TRIGGERS = cast(
    _TriggerMigration,
    import_module("agora.core.migrations.0009_rename_tables_to_tb_ta"),
)


class AlterExternalModelTable(Operation):
    """Change a model table in migration state without issuing duplicate DDL."""

    category = OperationCategory.ALTERATION
    reduces_to_sql = False

    def __init__(self, target_app_label: str, model_name: str, table: str) -> None:
        self.target_app_label = target_app_label
        self.model_name = model_name
        self.table = table

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        state.alter_model_options(
            self.target_app_label,
            self.model_name,
            {"db_table": self.table},
        )

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        return None

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        return None

    def describe(self) -> str:
        return (
            f"Set {self.target_app_label}.{self.model_name} migration-state table to {self.table}"
        )


def _rename_framework_tables(
    schema_editor: BaseDatabaseSchemaEditor,
    replacements: Mapping[str, str],
    *,
    prefixed: bool,
) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT table_name FROM user_tables")
        tables = {row[0] for row in cursor.fetchall()}
        missing = set(replacements) - tables
        conflicts = set(replacements.values()) & tables
        if missing or conflicts:
            detail = []
            if missing:
                detail.append(f"missing source tables: {', '.join(sorted(missing))}")
            if conflicts:
                detail.append(f"existing target tables: {', '.join(sorted(conflicts))}")
            raise RuntimeError("Cannot rename Django framework tables; " + "; ".join(detail))

        quote_name = schema_editor.connection.ops.quote_name
        for old_name, new_name in replacements.items():
            cursor.execute(f"ALTER TABLE {quote_name(old_name)} RENAME TO {quote_name(new_name)}")

    configure_django_runtime_table_names(prefixed=prefixed)
    configure_migration_recorder_table(prefixed=prefixed)


def _validate_current_triggers(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _TRIGGERS._retarget_triggers(schema_editor, {})


def _retarget_to_agora(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _TRIGGERS._retarget_triggers(schema_editor, DOMAIN_TABLE_RENAMES)


def _retarget_to_unscoped(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _TRIGGERS._retarget_triggers(
        schema_editor,
        {value: key for key, value in DOMAIN_TABLE_RENAMES.items()},
    )


def _rename_framework_to_agora(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_framework_tables(schema_editor, FRAMEWORK_TABLE_RENAMES, prefixed=True)


def _rename_framework_to_legacy(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_framework_tables(
        schema_editor,
        {value: key for key, value in FRAMEWORK_TABLE_RENAMES.items()},
        prefixed=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("persistence", "0009_rename_tables_to_tb_ta"),
        ("sessions", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(_validate_current_triggers, reverse_code=_retarget_to_unscoped),
        migrations.AlterModelTable(
            name="artifact",
            table="TB_TA_AGORA_ARTIFACT",
        ),
        migrations.AlterModelTable(
            name="auditevent",
            table="TB_TA_AGORA_AUDIT_EVENT",
        ),
        migrations.AlterModelTable(
            name="dashboard",
            table="TB_TA_AGORA_DASHBOARD",
        ),
        migrations.AlterModelTable(
            name="loginthrottle",
            table="TB_TA_AGORA_LOGIN_THROTTLE",
        ),
        migrations.AlterModelTable(
            name="renderauthorization",
            table="TB_TA_AGORA_RENDER_AUTHORIZATION",
        ),
        migrations.AlterModelTable(
            name="revision",
            table="TB_TA_AGORA_REVISION",
        ),
        migrations.AlterModelTable(
            name="storagereservation",
            table="TB_TA_AGORA_STORAGE_RESERVATION",
        ),
        migrations.AlterModelTable(
            name="user",
            table="TB_TA_AGORA_USER",
        ),
        migrations.AlterModelTable(
            name="viewergrant",
            table="TB_TA_AGORA_VIEWER_GRANT",
        ),
        AlterExternalModelTable(
            "contenttypes",
            "contenttype",
            "TB_TA_AGORA_DJANGO_CONTENT_TYPE",
        ),
        AlterExternalModelTable(
            "auth",
            "permission",
            "TB_TA_AGORA_AUTH_PERMISSION",
        ),
        AlterExternalModelTable(
            "auth",
            "group",
            "TB_TA_AGORA_AUTH_GROUP",
        ),
        AlterExternalModelTable(
            "sessions",
            "session",
            "TB_TA_AGORA_DJANGO_SESSION",
        ),
        migrations.RunPython(
            _rename_framework_to_agora,
            reverse_code=_rename_framework_to_legacy,
        ),
        migrations.RunPython(_retarget_to_agora, reverse_code=_validate_current_triggers),
    ]
