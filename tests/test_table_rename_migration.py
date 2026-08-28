from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any, cast

import pytest
from django.apps import apps
from django.db.migrations.state import ProjectState


class _Cursor:
    def __init__(
        self,
        source_names: set[str],
        *,
        errors: list[tuple[str, int, int, str]] | None = None,
        terminated: bool = True,
    ) -> None:
        self.source_names = source_names
        self.errors = errors or []
        self.terminated = terminated
        self.rows: list[tuple[Any, ...]] = []
        self.created: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, sql: str) -> None:
        if "FROM user_source" in sql:
            self.rows = [
                (
                    name,
                    f"TRIGGER {name} BEFORE INSERT ON persistence_user "
                    f"FOR EACH ROW BEGIN NULL; END{';' if self.terminated else ''}\n",
                )
                for name in sorted(self.source_names)
            ]
        elif "FROM user_errors" in sql:
            self.rows = list(self.errors)
        elif sql.startswith("CREATE OR REPLACE TRIGGER"):
            self.created.append(sql)
            self.rows = []
        else:  # pragma: no cover - protects the fake against an unexpected migration query.
            raise AssertionError(sql)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class _SchemaEditor:
    def __init__(self, cursor: _Cursor) -> None:
        self.connection = _Connection(cursor)


def _migration() -> ModuleType:
    return import_module("agora.persistence.migrations.0009_rename_tables_to_tb_ta")


def _project_prefix_migration() -> ModuleType:
    return import_module("agora.persistence.migrations.0010_apply_agora_project_table_prefix")


def test_table_rename_migration_retargets_every_trigger() -> None:
    migration = _migration()
    trigger_names = cast(frozenset[str], migration.TRIGGER_NAMES)
    cursor = _Cursor(set(trigger_names), terminated=False)

    migration._retarget_triggers(
        _SchemaEditor(cursor),
        {"persistence_user": "TB_TA_USER"},
    )

    assert len(cursor.created) == len(trigger_names)
    assert all("TB_TA_USER" in statement for statement in cursor.created)
    assert all("persistence_user" not in statement for statement in cursor.created)
    assert all(statement.endswith(";\n") for statement in cursor.created)


def test_table_rename_migration_fails_closed_for_missing_or_invalid_triggers() -> None:
    migration = _migration()
    trigger_names = cast(frozenset[str], migration.TRIGGER_NAMES)
    missing_cursor = _Cursor(set(trigger_names) - {"AGORA_USER_RETENTION_GUARD"})
    with pytest.raises(RuntimeError, match="missing Oracle triggers"):
        migration._retarget_triggers(_SchemaEditor(missing_cursor), {})

    invalid_cursor = _Cursor(
        set(trigger_names),
        errors=[("AGORA_USER_RETENTION_GUARD", 2, 5, "simulated compile error")],
    )
    with pytest.raises(RuntimeError, match="simulated compile error"):
        migration._retarget_triggers(_SchemaEditor(invalid_cursor), {})


def test_table_rename_migration_direction_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        migration, "_retarget_triggers", lambda editor, mapping: calls.append(mapping)
    )
    schema_editor = object()

    migration._retarget_to_tb_ta(None, schema_editor)
    migration._retarget_to_persistence(None, schema_editor)
    migration._validate_current_triggers(None, schema_editor)

    table_renames = cast(dict[str, str], migration.TABLE_RENAMES)
    assert calls == [
        table_renames,
        {value: key for key, value in table_renames.items()},
        {},
    ]


class _TableCursor:
    def __init__(self, tables: set[str]) -> None:
        self.tables = tables
        self.rows: list[tuple[str]] = []
        self.executed: list[str] = []

    def __enter__(self) -> _TableCursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if sql == "SELECT table_name FROM user_tables":
            self.rows = [(name,) for name in sorted(self.tables)]

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class _TableOperations:
    @staticmethod
    def quote_name(name: str) -> str:
        return f'"{name}"'


class _TableConnection:
    ops = _TableOperations()

    def __init__(self, cursor: _TableCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _TableCursor:
        return self._cursor


class _TableSchemaEditor:
    def __init__(self, cursor: _TableCursor) -> None:
        self.connection = _TableConnection(cursor)


def test_project_prefix_migration_renames_framework_tables_and_runtime_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _project_prefix_migration()
    replacements = {
        "AUTH_GROUP": "TB_TA_AGORA_AUTH_GROUP",
        "DJANGO_MIGRATIONS": "TB_TA_AGORA_DJANGO_MIGRATIONS",
    }
    cursor = _TableCursor(set(replacements))
    schema_editor = _TableSchemaEditor(cursor)
    runtime_calls: list[bool] = []
    recorder_calls: list[bool] = []
    monkeypatch.setattr(
        migration,
        "configure_django_runtime_table_names",
        lambda *, prefixed: runtime_calls.append(prefixed),
    )
    monkeypatch.setattr(
        migration,
        "configure_migration_recorder_table",
        lambda *, prefixed: recorder_calls.append(prefixed),
    )

    migration._rename_framework_tables(schema_editor, replacements, prefixed=True)

    assert cursor.executed == [
        "SELECT table_name FROM user_tables",
        'ALTER TABLE "AUTH_GROUP" RENAME TO "TB_TA_AGORA_AUTH_GROUP"',
        ('ALTER TABLE "DJANGO_MIGRATIONS" RENAME TO "TB_TA_AGORA_DJANGO_MIGRATIONS"'),
    ]
    assert runtime_calls == [True]
    assert recorder_calls == [True]


def test_project_prefix_migration_fails_closed_for_missing_or_conflicting_tables() -> None:
    migration = _project_prefix_migration()
    replacements = {"AUTH_GROUP": "TB_TA_AGORA_AUTH_GROUP"}
    cursor = _TableCursor({"TB_TA_AGORA_AUTH_GROUP"})
    schema_editor = _TableSchemaEditor(cursor)

    with pytest.raises(
        RuntimeError,
        match=("missing source tables: AUTH_GROUP; existing target tables: TB_TA_AGORA_AUTH_GROUP"),
    ):
        migration._rename_framework_tables(schema_editor, replacements, prefixed=True)


def test_project_prefix_migration_updates_external_model_state() -> None:
    migration = _project_prefix_migration()
    operation = migration.AlterExternalModelTable(
        "contenttypes",
        "contenttype",
        "TB_TA_AGORA_DJANGO_CONTENT_TYPE",
    )
    state = ProjectState.from_apps(apps)

    operation.state_forwards("persistence", state)

    assert (
        state.models["contenttypes", "contenttype"].options["db_table"]
        == "TB_TA_AGORA_DJANGO_CONTENT_TYPE"
    )
    assert operation.database_forwards("persistence", cast(Any, None), state, state) is None
    assert operation.database_backwards("persistence", cast(Any, None), state, state) is None
    assert "contenttypes.contenttype" in operation.describe()


def test_project_prefix_migration_direction_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _project_prefix_migration()
    trigger_calls: list[dict[str, str]] = []
    framework_calls: list[tuple[dict[str, str], bool]] = []
    monkeypatch.setattr(
        migration._TRIGGERS,
        "_retarget_triggers",
        lambda editor, mapping: trigger_calls.append(dict(mapping)),
    )
    monkeypatch.setattr(
        migration,
        "_rename_framework_tables",
        lambda editor, mapping, *, prefixed: framework_calls.append((dict(mapping), prefixed)),
    )
    schema_editor = cast(Any, object())

    migration._validate_current_triggers(None, schema_editor)
    migration._retarget_to_agora(None, schema_editor)
    migration._retarget_to_unscoped(None, schema_editor)
    migration._rename_framework_to_agora(None, schema_editor)
    migration._rename_framework_to_legacy(None, schema_editor)

    domain_renames = cast(dict[str, str], migration.DOMAIN_TABLE_RENAMES)
    framework_renames = cast(dict[str, str], migration.FRAMEWORK_TABLE_RENAMES)
    assert trigger_calls == [
        {},
        domain_renames,
        {value: key for key, value in domain_renames.items()},
    ]
    assert framework_calls == [
        (framework_renames, True),
        ({value: key for key, value in framework_renames.items()}, False),
    ]
