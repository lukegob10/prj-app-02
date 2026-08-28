"""Rename Agora tables and retarget their Oracle triggers."""

import re
from collections import defaultdict
from collections.abc import Mapping

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

TABLE_RENAMES = {
    "persistence_artifact": "TB_TA_ARTIFACT",
    "persistence_auditevent": "TB_TA_AUDIT_EVENT",
    "persistence_dashboard": "TB_TA_DASHBOARD",
    "persistence_loginthrottle": "TB_TA_LOGIN_THROTTLE",
    "persistence_renderauthoriz227c": "TB_TA_RENDER_AUTHORIZATION",
    "persistence_revision": "TB_TA_REVISION",
    "persistence_storagereservation": "TB_TA_STORAGE_RESERVATION",
    "persistence_user": "TB_TA_USER",
    "persistence_viewergrant": "TB_TA_VIEWER_GRANT",
}
TRIGGER_NAMES = frozenset(
    {
        "AGORA_ARTIFACT_IMMUT_GUARD",
        "AGORA_ARTIFACT_INSERT_GUARD",
        "AGORA_AUDIT_APPEND_GUARD",
        "AGORA_DASHBOARD_GUARD",
        "AGORA_DASHBOARD_LATEST_GUARD",
        "AGORA_GRANT_IMMUT_GUARD",
        "AGORA_RESERVATION_MUT_GUARD",
        "AGORA_REVISION_AUTH_GUARD",
        "AGORA_REVISION_COMPLETE_GUARD",
        "AGORA_REVISION_IMMUT_GUARD",
        "AGORA_USER_AUTH_VERSION_GUARD",
        "AGORA_USER_RETENTION_GUARD",
    }
)


def _retarget_triggers(
    schema_editor: BaseDatabaseSchemaEditor,
    replacements: Mapping[str, str],
) -> None:
    source_by_name: defaultdict[str, list[str]] = defaultdict(list)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT name, text
            FROM user_source
            WHERE type = 'TRIGGER'
            ORDER BY name, line
            """
        )
        for name, text in cursor.fetchall():
            if name in TRIGGER_NAMES:
                source_by_name[name].append(text)

        missing = TRIGGER_NAMES - source_by_name.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(f"Cannot retarget missing Oracle triggers: {names}")

        for name in sorted(TRIGGER_NAMES):
            source = "".join(source_by_name[name])
            for old_name, new_name in replacements.items():
                source = re.sub(
                    re.escape(old_name),
                    new_name,
                    source,
                    flags=re.IGNORECASE,
                )
            source = source.rstrip()
            if not source.endswith(";"):
                source = f"{source};"
            # Django's Oracle cursor removes a final semicolon when it is the query's
            # last character. A trailing newline preserves the required PL/SQL terminator.
            cursor.execute(f"CREATE OR REPLACE {source}\n")

        cursor.execute(
            """
            SELECT name, line, position, text
            FROM user_errors
            WHERE type = 'TRIGGER'
            ORDER BY name, sequence
            """
        )
        errors = [row for row in cursor.fetchall() if row[0] in TRIGGER_NAMES]
        if errors:
            detail = "; ".join(
                f"{name}:{line}:{position} {text}" for name, line, position, text in errors
            )
            raise RuntimeError(f"Oracle trigger retargeting failed: {detail}")


def _retarget_to_tb_ta(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _retarget_triggers(schema_editor, TABLE_RENAMES)


def _retarget_to_persistence(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _retarget_triggers(schema_editor, {value: key for key, value in TABLE_RENAMES.items()})


def _validate_current_triggers(_apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _retarget_triggers(schema_editor, {})


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0008_renderauthorization"),
    ]

    operations = [
        migrations.RunPython(_validate_current_triggers, reverse_code=_retarget_to_persistence),
        migrations.AlterModelTable(
            name="artifact",
            table="TB_TA_ARTIFACT",
        ),
        migrations.AlterModelTable(
            name="auditevent",
            table="TB_TA_AUDIT_EVENT",
        ),
        migrations.AlterModelTable(
            name="dashboard",
            table="TB_TA_DASHBOARD",
        ),
        migrations.AlterModelTable(
            name="loginthrottle",
            table="TB_TA_LOGIN_THROTTLE",
        ),
        migrations.AlterModelTable(
            name="renderauthorization",
            table="TB_TA_RENDER_AUTHORIZATION",
        ),
        migrations.AlterModelTable(
            name="revision",
            table="TB_TA_REVISION",
        ),
        migrations.AlterModelTable(
            name="storagereservation",
            table="TB_TA_STORAGE_RESERVATION",
        ),
        migrations.AlterModelTable(
            name="user",
            table="TB_TA_USER",
        ),
        migrations.AlterModelTable(
            name="viewergrant",
            table="TB_TA_VIEWER_GRANT",
        ),
        migrations.RunPython(_retarget_to_tb_ta, reverse_code=_validate_current_triggers),
    ]
