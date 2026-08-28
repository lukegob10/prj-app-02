from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.db.migrations.recorder import MigrationRecorder

from agora.db import table_names
from agora.db.backends.treasury_oracle.operations import DatabaseOperations


class _RecorderCursor:
    def __init__(self, count: int) -> None:
        self.count = count
        self.executed: list[tuple[str, dict[str, str]]] = []

    def __enter__(self) -> _RecorderCursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, sql: str, parameters: dict[str, str]) -> None:
        self.executed.append((sql, parameters))

    def fetchone(self) -> tuple[int]:
        return (self.count,)


class _RecorderConnection:
    def __init__(self, cursor: _RecorderCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecorderCursor:
        return self._cursor


def test_runtime_framework_models_use_project_table_namespace() -> None:
    assert Group._meta.db_table == "TB_TA_AGORA_AUTH_GROUP"
    assert Group.permissions.through._meta.db_table == "TB_TA_AGORA_AUTH_GROUP_PERMISSIONS"
    assert Permission._meta.db_table == "TB_TA_AGORA_AUTH_PERMISSION"
    assert ContentType._meta.db_table == "TB_TA_AGORA_DJANGO_CONTENT_TYPE"
    assert Session._meta.db_table == "TB_TA_AGORA_DJANGO_SESSION"


def test_runtime_framework_table_configuration_is_reversible() -> None:
    try:
        table_names.configure_django_runtime_table_names(prefixed=False)

        assert Group._meta.db_table == "auth_group"
        assert "db_table" not in Group._meta.original_attrs
        assert Permission._meta.db_table == "auth_permission"
        assert "db_table" not in Permission._meta.original_attrs
        assert ContentType._meta.db_table == "django_content_type"
        assert Session._meta.db_table == "django_session"
    finally:
        table_names.configure_django_runtime_table_names(prefixed=True)


def test_runtime_configuration_skips_framework_apps_that_are_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cast(Any, table_names).apps, "is_installed", lambda _name: False)

    table_names.configure_django_runtime_table_names(prefixed=True)


@pytest.mark.parametrize(("table_count", "prefixed"), [(0, False), (1, True)])
def test_migration_recorder_selects_the_physical_table(
    monkeypatch: pytest.MonkeyPatch,
    table_count: int,
    prefixed: bool,
) -> None:
    cursor = _RecorderCursor(table_count)
    configured: list[bool] = []
    monkeypatch.setattr(table_names, "_recorder_table_selected", False)
    monkeypatch.setattr(
        table_names,
        "configure_migration_recorder_table",
        lambda *, prefixed: configured.append(prefixed),
    )

    table_names.select_migration_recorder_table(_RecorderConnection(cursor))

    assert configured == [prefixed]
    assert cursor.executed == [
        (
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :table_name",
            {"table_name": "TB_TA_AGORA_DJANGO_MIGRATIONS"},
        )
    ]


def test_migration_recorder_selection_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table_names, "_recorder_table_selected", True)

    table_names.select_migration_recorder_table(cast(Any, None))


def test_migration_recorder_configuration_uses_introspection_compatible_case() -> None:
    try:
        table_names.configure_migration_recorder_table(prefixed=False)
        assert MigrationRecorder.Migration._meta.db_table == "django_migrations"

        table_names.configure_migration_recorder_table(prefixed=True)
        assert MigrationRecorder.Migration._meta.db_table == "tb_ta_agora_django_migrations"
    finally:
        table_names.configure_migration_recorder_table(prefixed=True)


def test_oracle_operations_preserve_the_project_namespace() -> None:
    operations = DatabaseOperations(cast(Any, None))

    assert (
        operations.quote_name("tb_ta_agora_django_content_type")
        == '"TB_TA_AGORA_DJANGO_CONTENT_TYPE"'
    )
    assert (
        operations.quote_name("persistence_renderauthorization")
        == '"PERSISTENCE_RENDERAUTHORIZ227C"'
    )
