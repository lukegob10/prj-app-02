"""Generate the checked-in Oracle schema from Django's migration state.

The migration graph is the source of truth.  This command materializes its final state with
Agora's real Django Oracle backend and carries forward migration-owned Oracle SQL (constraints,
function-based indexes, and triggers).  It never opens a database connection.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCHEMA_PATH: Final[Path] = _REPOSITORY_ROOT / "database" / "oracle" / "schema.sql"
_DJANGO_SETTINGS_MODULE: Final[str] = "agora.settings.portal"

# The settings module validates runtime configuration before Django can be initialized.  These
# values are deliberately local/offline-only and are overwritten in this process so the output
# does not depend on an operator's credentials, profile, or .env file.
_OFFLINE_ENVIRONMENT: Final[dict[str, str]] = {
    "AGORA_ENVIRONMENT": "test",
    "AGORA_DEBUG": "false",
    "AGORA_PORTAL_ORIGIN": "https://portal.agora.test",
    "AGORA_CONTENT_ORIGIN": "https://content.agora.test",
    "AGORA_PORTAL_SECRET_KEY": "p" * 64,
    "AGORA_CONTENT_SECRET_KEY": "c" * 64,
    "ENV": "PROD",
    "TA_PROD_PASSWORD": "offline-schema-generation",
    "AGORA_ARTIFACT_ROOT": str(_REPOSITORY_ROOT / ".local" / "schema-generation"),
}

_TRIGGER_RE: Final[re.Pattern[str]] = re.compile(
    r"^CREATE\s+OR\s+REPLACE\s+TRIGGER\s+([A-Z0-9_$#]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
_INDEX_RE: Final[re.Pattern[str]] = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+([A-Z0-9_$#]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
_ADD_CONSTRAINT_RE: Final[re.Pattern[str]] = re.compile(
    r"^ALTER\s+TABLE\s+[^\s]+\s+ADD\s+CONSTRAINT\s+([A-Z0-9_$#]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
_DROP_INDEX_RE: Final[re.Pattern[str]] = re.compile(
    r"^DROP\s+INDEX\s+([A-Z0-9_$#]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
_DROP_CONSTRAINT_RE: Final[re.Pattern[str]] = re.compile(
    r"^ALTER\s+TABLE\s+[^\s]+\s+DROP\s+CONSTRAINT\s+([A-Z0-9_$#]+)",
    flags=re.IGNORECASE | re.DOTALL,
)
_DATA_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:INSERT|UPDATE|DELETE|MERGE)\b",
    flags=re.IGNORECASE | re.DOTALL,
)

_KNOWN_RUNPYTHON_BY_MIGRATION: Final[dict[str, frozenset[str]]] = {
    "auth.0011_update_proxy_permissions": frozenset({"update_proxy_model_permissions"}),
    "contenttypes.0002_remove_content_type_name": frozenset({"noop"}),
    "persistence.0009_rename_tables_to_tb_ta": frozenset(
        {"_retarget_to_tb_ta", "_validate_current_triggers"}
    ),
    "persistence.0010_apply_agora_project_table_prefix": frozenset(
        {"_rename_framework_to_agora", "_retarget_to_agora", "_validate_current_triggers"}
    ),
    "persistence.0012_enhancement_core_schema": frozenset({"_backfill_publication_versions"}),
    "persistence.0013_ownership_transfer_invariants": frozenset({"noop"}),
    "persistence.0014_usage_analytics_schema": frozenset({"_backfill_viewer_publication_versions"}),
}


class UnsupportedMigrationOperation(RuntimeError):
    """Raised when a migration contains executable work this DDL generator cannot model."""


@dataclass(frozen=True, slots=True)
class RawSqlEntry:
    """One final migration-owned SQL object and its provenance."""

    name: str
    sql: str
    migration: str


@dataclass(frozen=True, slots=True)
class RawSqlObjects:
    """Latest forward definition for each migration-owned Oracle object."""

    constraints: tuple[RawSqlEntry, ...]
    indexes: tuple[RawSqlEntry, ...]
    triggers: tuple[RawSqlEntry, ...]
    skipped: tuple[str, ...]


def _prepare_offline_settings() -> None:
    """Provide deterministic settings before importing Django's settings module."""

    os.environ.update(_OFFLINE_ENVIRONMENT)
    os.environ["DJANGO_SETTINGS_MODULE"] = _DJANGO_SETTINGS_MODULE


def _load_django() -> tuple[Any, Any, Any, Any]:
    """Initialize Django and return the loaded migration graph, final state, and connection."""

    # Importing Django's Oracle backend may set NLS_LANG and ORA_NCHAR_LITERAL_REPLACE as well as
    # the settings keys above. Snapshot the complete process environment so an imported helper
    # cannot leak credentials, profile values, or driver defaults into a caller/test process.
    original_environment = dict(os.environ)
    try:
        _prepare_offline_settings()

        import django
        from django.db import connections
        from django.db.migrations.loader import MigrationLoader

        django.setup()
        loader = MigrationLoader(None, replace_migrations=False)
        state = loader.project_state()
        connection = connections["default"]

        # Oracle's operators descriptor probes a live connection the first time a check constraint
        # is compiled.  The standard operators are stable backend constants and are sufficient for
        # SQL rendering; assigning them is what makes schema_editor(collect_sql=True) truly offline.
        connection.operators = connection._standard_operators
        connection.pattern_ops = connection._standard_pattern_ops
        return loader, state, connection, django
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


def _ordered_migration_keys(loader: Any) -> tuple[tuple[str, str], ...]:
    """Return every installed migration reachable from every leaf in stable topological order."""

    graph = loader.graph
    required: set[tuple[str, str]] = set()
    for leaf in sorted(graph.leaf_nodes()):
        required.update(graph.forwards_plan(leaf))

    ordered: list[tuple[str, str]] = []
    remaining = set(required)
    while remaining:
        ready = sorted(
            key
            for key in remaining
            if all(parent.key not in remaining for parent in graph.node_map[key].parents)
        )
        if not ready:
            raise RuntimeError("Django migration graph could not be topologically ordered")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return tuple(ordered)


def _flatten_database_operations(operation: Any) -> Iterator[Any]:
    """Yield database operations, including the database side of SeparateDatabaseAndState."""

    from django.db.migrations.operations.special import SeparateDatabaseAndState

    if isinstance(operation, SeparateDatabaseAndState):
        yield from operation.database_operations
    else:
        yield operation


def _sql_values(sql: Any) -> Iterator[str]:
    """Yield RunSQL strings without treating a single string as a character sequence."""

    if sql is None:
        return
    if isinstance(sql, str):
        if sql.strip():
            yield sql
        return
    if isinstance(sql, Sequence):
        for value in sql:
            if not isinstance(value, str):
                raise UnsupportedMigrationOperation(
                    f"RunSQL contains a non-string SQL value: {type(value).__name__}"
                )
            if value.strip():
                yield value
        return
    raise UnsupportedMigrationOperation(
        f"RunSQL contains unsupported SQL type: {type(sql).__name__}"
    )


def _normalise_sql(sql: str) -> str:
    """Normalize line endings and outer whitespace without changing Oracle SQL semantics."""

    return sql.replace("\r\n", "\n").replace("\r", "\n").strip()


def _migration_label(key: tuple[str, str]) -> str:
    return f"{key[0]}.{key[1]}"


def _is_known_runpython(label: str, operation: Any) -> bool:
    code = getattr(operation, "code", None)
    name = getattr(code, "__name__", "")
    return name in _KNOWN_RUNPYTHON_BY_MIGRATION.get(label, frozenset())


def _final_table_names(state: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Build historical-to-final identifier mappings from Django's final migration state."""

    default_to_final: dict[str, str] = {}
    legacy_to_final: dict[str, str] = {}
    for model in state.apps.get_models(include_auto_created=True):
        table = str(model._meta.db_table).upper()
        default = f"{model._meta.app_label}_{model._meta.model_name}".upper()
        default_to_final[default] = table
        if table.startswith("TB_TA_AGORA_"):
            legacy_to_final[f"TB_TA_{table.removeprefix('TB_TA_AGORA_')}"] = table

    # Migration 0010 intentionally keeps the physical through-table name longer than Django's
    # generated Oracle identifier.  The runtime table-name adapter is authoritative for this
    # one external framework table, so make the artifact use that same name.
    default_to_final["AUTH_GROUP_PERMISSIONS"] = "TB_TA_AGORA_AUTH_GROUP_PERMISSIONS"

    return default_to_final, legacy_to_final


def _replace_identifiers(
    sql: str,
    *,
    default_to_final: dict[str, str],
    legacy_to_final: dict[str, str],
) -> str:
    """Retarget historical unprefixed/legacy identifiers to the final project namespace."""

    replacements = {**default_to_final, **legacy_to_final}
    if not replacements:
        return sql
    pattern = re.compile(
        r"(?<![A-Z0-9_$#])(?:"
        + "|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True))
        + r")(?![A-Z0-9_$#])",
        flags=re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        return replacements[match.group(0).upper()]

    return pattern.sub(replace, sql)


def _collect_raw_sql(loader: Any, state: Any, keys: Iterable[tuple[str, str]]) -> RawSqlObjects:
    """Collect latest migration-owned Oracle objects and reject unknown executable work."""

    from django.db.migrations.operations.special import RunPython, RunSQL

    default_to_final, legacy_to_final = _final_table_names(state)
    constraints: dict[str, RawSqlEntry] = {}
    indexes: dict[str, RawSqlEntry] = {}
    triggers: dict[str, RawSqlEntry] = {}
    skipped: list[str] = []

    for key in keys:
        migration = loader.graph.nodes[key]
        label = _migration_label(key)
        for operation in migration.operations:
            for database_operation in _flatten_database_operations(operation):
                if isinstance(database_operation, RunPython):
                    if not _is_known_runpython(label, database_operation):
                        name = getattr(
                            getattr(database_operation, "code", None), "__name__", "<callable>"
                        )
                        raise UnsupportedMigrationOperation(
                            f"{label} contains unsupported RunPython operation {name}; "
                            "DDL generation cannot execute data or schema Python code offline"
                        )
                    name = getattr(
                        getattr(database_operation, "code", None), "__name__", "<callable>"
                    )
                    skipped.append(f"{label}: RunPython {name} (applied by Django migrations)")
                    continue

                if not isinstance(database_operation, RunSQL):
                    continue

                for original_sql in _sql_values(database_operation.sql):
                    sql = _replace_identifiers(
                        _normalise_sql(original_sql),
                        default_to_final=default_to_final,
                        legacy_to_final=legacy_to_final,
                    )
                    upper_sql = sql.upper()

                    trigger_match = _TRIGGER_RE.match(sql)
                    if trigger_match:
                        name = trigger_match.group(1).upper()
                        triggers[name] = RawSqlEntry(name, sql, label)
                        continue

                    index_match = _INDEX_RE.match(sql)
                    if index_match:
                        name = index_match.group(1).upper()
                        indexes[name] = RawSqlEntry(name, sql, label)
                        continue

                    constraint_match = _ADD_CONSTRAINT_RE.match(sql)
                    if constraint_match:
                        name = constraint_match.group(1).upper()
                        constraints[name] = RawSqlEntry(name, sql, label)
                        continue

                    drop_index_match = _DROP_INDEX_RE.match(sql)
                    if drop_index_match:
                        indexes.pop(drop_index_match.group(1).upper(), None)
                        continue

                    drop_constraint_match = _DROP_CONSTRAINT_RE.match(sql)
                    if drop_constraint_match:
                        constraints.pop(drop_constraint_match.group(1).upper(), None)
                        continue

                    if _DATA_SQL_RE.match(sql):
                        skipped.append(f"{label}: data SQL (applied by Django migrations)")
                        continue

                    # 0017 conditionally drops historical checks.  They are already represented
                    # by final-state AddConstraint output, so no equivalent install DDL exists.
                    if upper_sql.startswith("DECLARE") and "DROP CONSTRAINT" in upper_sql:
                        skipped.append(f"{label}: conditional historical constraint cleanup")
                        continue

                    raise UnsupportedMigrationOperation(
                        f"{label} contains unsupported RunSQL: {sql.splitlines()[0][:120]}"
                    )

    return RawSqlObjects(
        constraints=tuple(sorted(constraints.values(), key=lambda entry: entry.name)),
        indexes=tuple(sorted(indexes.values(), key=lambda entry: entry.name)),
        triggers=tuple(sorted(triggers.values(), key=lambda entry: entry.name)),
        skipped=tuple(skipped),
    )


def _create_model_sql(state: Any, connection: Any) -> tuple[str, ...]:
    """Render final-state tables, identities, model constraints, FKs, and indexes offline."""

    from django.db.migrations.recorder import MigrationRecorder

    # Keep the framework through table in lockstep with agora.db.table_names.py without importing
    # the application package path that may be renamed while retaining the persistence app label.
    group = state.apps.get_model("auth", "group")
    permissions_field = group._meta.get_field("permissions")
    permissions_through = permissions_field.remote_field.through
    permissions_through._meta.db_table = "TB_TA_AGORA_AUTH_GROUP_PERMISSIONS"

    MigrationRecorder.Migration._meta.db_table = "tb_ta_agora_django_migrations"
    models = list(state.apps.get_models())
    models.append(MigrationRecorder.Migration)

    statements: list[str] = []
    with connection.schema_editor(collect_sql=True, atomic=False) as schema_editor:
        for model in models:
            if model._meta.can_migrate(connection):
                schema_editor.create_model(model)
    statements.extend(schema_editor.collected_sql)
    return tuple(_normalise_sql(statement) for statement in statements)


def _format_custom_sql(entry: RawSqlEntry) -> str:
    """Format migration-owned SQL for SQL*Plus-compatible execution."""

    sql = _normalise_sql(entry.sql)
    if _TRIGGER_RE.match(sql):
        if not sql.endswith(";"):
            sql = f"{sql};"
        return f"{sql}\n/"
    if not sql.endswith(";"):
        return f"{sql};"
    return sql


def render_schema() -> str:
    """Return the deterministic final schema artifact contents."""

    loader, state, connection, django = _load_django()
    keys = _ordered_migration_keys(loader)
    raw = _collect_raw_sql(loader, state, keys)
    generated_sql = _create_model_sql(state, connection)

    lines = [
        "-- Agora Oracle schema artifact (generated; do not edit by hand).",
        "-- Source of truth: Django migrations; this file is a reproducible final-state DDL view.",
        "-- Generator: uv run --locked python scripts/generate_oracle_schema.py",
        "-- Backend: agora.db.backends.treasury_oracle (Django Oracle schema editor).",
        f"-- Django version: {django.get_version()} (pyproject constraint >=5.2.17,<5.3).",
        "-- Generation is offline: no Oracle connection, credentials, or data migrations are used.",
        "-- Tables, identities, model constraints, foreign keys, and model indexes come directly",
        "-- from the final ProjectState; migration-owned Oracle SQL follows below.",
        "-- Run Django migrations for upgrades/data backfills; do not edit this artifact as a",
        "-- second source of truth. Re-run the generator after changing migrations.",
        "-- Canonical application package may move to agora.core; Django app/migration label stays",
        "-- persistence for migration identity and historical dependency resolution.",
        "",
        "-- Installed apps without migrations (and therefore without schema objects):",
        "--   " + ", ".join(sorted(loader.unmigrated_apps)),
        "",
        "-- Final migration graph:",
    ]
    lines.extend(f"--   {_migration_label(key)}" for key in keys)
    lines.extend(
        [
            "",
            "-- Django final-state schema (tables, identity columns, constraints, foreign keys,",
            "-- indexes).",
        ]
    )
    lines.extend(generated_sql)
    lines.extend(["", "-- Migration-owned Oracle constraints."])
    lines.extend(_format_custom_sql(entry) for entry in raw.constraints)
    lines.extend(["", "-- Migration-owned Oracle function-based indexes."])
    lines.extend(_format_custom_sql(entry) for entry in raw.indexes)
    lines.extend(["", "-- Migration-owned Oracle triggers."])
    lines.extend(_format_custom_sql(entry) for entry in raw.triggers)
    lines.extend(
        ["", "-- Explicitly deferred operations (handled by Django migrations during upgrade):"]
    )
    lines.extend(f"--   {item}" for item in raw.skipped)
    lines.append("")
    return "\n".join(lines)


def _relative_schema_path() -> str:
    try:
        return _SCHEMA_PATH.relative_to(_REPOSITORY_ROOT).as_posix()
    except ValueError:
        return _SCHEMA_PATH.as_posix()


def _write_schema(contents: str) -> None:
    """Atomically replace the generated artifact while preserving UTF-8/LF output."""

    _SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _SCHEMA_PATH.with_name(f".{_SCHEMA_PATH.name}.tmp")
    try:
        temporary_path.write_text(contents, encoding="utf-8", newline="\n")
        temporary_path.replace(_SCHEMA_PATH)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    """Generate or check the checked-in Oracle schema artifact."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in artifact differs from deterministic generation",
    )
    args = parser.parse_args(argv)

    try:
        contents = render_schema()
    except (UnsupportedMigrationOperation, RuntimeError) as error:
        print(f"Oracle schema generation failed: {error}", file=sys.stderr)
        return 2

    if args.check:
        try:
            current = _SCHEMA_PATH.read_text(encoding="utf-8", newline="")
        except FileNotFoundError:
            current = None
        if current == contents:
            print(f"Oracle schema artifact is up to date: {_relative_schema_path()}")
            return 0
        print(
            "Oracle schema artifact is out of date; run "
            "uv run --locked python scripts/generate_oracle_schema.py",
            file=sys.stderr,
        )
        return 1

    _write_schema(contents)
    print(f"Generated Oracle schema artifact: {_relative_schema_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
