"""Anchor trigger UUID variables to Django's Oracle UUID column types."""

from importlib import import_module
from typing import Final, cast

from django.db import migrations

_CORE_MIGRATION: Final = "agora.core.migrations.0012_enhancement_core_schema"
_TRANSFER_MIGRATION: Final = "agora.core.migrations.0013_ownership_transfer_invariants"
_ANALYTICS_MIGRATION: Final = "agora.core.migrations.0014_usage_analytics_schema"


def _load_sql(module_name: str, constant_name: str) -> str:
    module = import_module(module_name)
    return cast(str, getattr(module, constant_name))


def _replace_exact(sql: str, replacements: tuple[tuple[str, str], ...]) -> str:
    corrected = sql
    for old, new in replacements:
        if corrected.count(old) != 1:
            raise RuntimeError(f"expected exactly one trigger declaration: {old}")
        corrected = corrected.replace(old, new)
    return corrected


ORIGINAL_ACCESS_REQUEST_GUARD_SQL = _load_sql(_CORE_MIGRATION, "ACCESS_REQUEST_TRIGGER_SQL")
ACCESS_REQUEST_GUARD_SQL = _replace_exact(
    ORIGINAL_ACCESS_REQUEST_GUARD_SQL,
    (
        (
            "current_owner RAW(16);",
            "current_owner TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE;",
        ),
    ),
)

ORIGINAL_DASHBOARD_TRANSFER_GUARD_SQL = _load_sql(
    _TRANSFER_MIGRATION, "DASHBOARD_TRANSFER_GUARD_SQL"
)
DASHBOARD_TRANSFER_GUARD_SQL = _replace_exact(
    ORIGINAL_DASHBOARD_TRANSFER_GUARD_SQL,
    (
        (
            "marker_dashboard RAW(16);",
            "marker_dashboard TB_TA_AGORA_DASH_TRANSFER.DASHBOARD_ID%TYPE;",
        ),
        (
            "marker_from_owner RAW(16);",
            "marker_from_owner TB_TA_AGORA_DASH_TRANSFER.FROM_OWNER_ID%TYPE;",
        ),
        (
            "marker_to_owner RAW(16);",
            "marker_to_owner TB_TA_AGORA_DASH_TRANSFER.TO_OWNER_ID%TYPE;",
        ),
        (
            "marker_previous RAW(16);",
            "marker_previous TB_TA_AGORA_DASH_TRANSFER.PREVIOUS_TRANSFER_ID%TYPE;",
        ),
    ),
)

ORIGINAL_REVISION_AUTHORIZATION_GUARD_SQL = _load_sql(
    _TRANSFER_MIGRATION, "REVISION_AUTHORIZATION_GUARD_SQL"
)
REVISION_AUTHORIZATION_GUARD_SQL = _replace_exact(
    ORIGINAL_REVISION_AUTHORIZATION_GUARD_SQL,
    (
        (
            "current_owner RAW(16);",
            "current_owner TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE;",
        ),
    ),
)

ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL = _load_sql(
    _TRANSFER_MIGRATION, "GRANT_AUTHORIZATION_GUARD_SQL"
)
GRANT_AUTHORIZATION_GUARD_SQL = _replace_exact(
    ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL,
    (
        (
            "current_owner RAW(16);",
            "current_owner TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE;",
        ),
    ),
)

ORIGINAL_TRANSFER_HISTORY_GUARD_SQL = _load_sql(_TRANSFER_MIGRATION, "TRANSFER_HISTORY_GUARD_SQL")
TRANSFER_HISTORY_GUARD_SQL = _replace_exact(
    ORIGINAL_TRANSFER_HISTORY_GUARD_SQL,
    (
        (
            "current_owner RAW(16);",
            "current_owner TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE;",
        ),
        (
            "current_marker RAW(16);",
            "current_marker TB_TA_AGORA_DASHBOARD.LAST_OWNERSHIP_TRANSFER_ID%TYPE;",
        ),
    ),
)

ORIGINAL_AUTHORIZED_OPEN_GUARD_SQL = _load_sql(_ANALYTICS_MIGRATION, "AUTHORIZED_OPEN_GUARD_SQL")
AUTHORIZED_OPEN_GUARD_SQL = _replace_exact(
    ORIGINAL_AUTHORIZED_OPEN_GUARD_SQL,
    (
        (
            "source_dashboard RAW(16);",
            "source_dashboard TB_TA_AGORA_RENDER_AUTHORIZATION.DASHBOARD_ID%TYPE;",
        ),
        (
            "source_viewer RAW(16);",
            "source_viewer TB_TA_AGORA_RENDER_AUTHORIZATION.VIEWER_ID%TYPE;",
        ),
        (
            "source_revision RAW(16);",
            "source_revision TB_TA_AGORA_RENDER_AUTHORIZATION.REVISION_ID%TYPE;",
        ),
        (
            "source_grant RAW(16);",
            "source_grant TB_TA_AGORA_RENDER_AUTHORIZATION.VIEWER_GRANT_ID%TYPE;",
        ),
        (
            "source_owner_epoch RAW(16);",
            "source_owner_epoch TB_TA_AGORA_RENDER_AUTHORIZATION.OWNER_TRANSFER_EPOCH_ID%TYPE;",
        ),
        (
            "dashboard_owner RAW(16);",
            "dashboard_owner TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE;",
        ),
        (
            "dashboard_owner_epoch RAW(16);",
            "dashboard_owner_epoch TB_TA_AGORA_DASHBOARD.LAST_OWNERSHIP_TRANSFER_ID%TYPE;",
        ),
        (
            "dashboard_revision RAW(16);",
            "dashboard_revision TB_TA_AGORA_DASHBOARD.PUBLISHED_REVISION_ID%TYPE;",
        ),
    ),
)

ORIGINAL_AUTHORIZED_OPEN_MARK_TRIGGER_SQL = _load_sql(
    _ANALYTICS_MIGRATION, "AUTHORIZED_OPEN_MARK_TRIGGER_SQL"
)
AUTHORIZED_OPEN_MARK_TRIGGER_SQL = _replace_exact(
    ORIGINAL_AUTHORIZED_OPEN_MARK_TRIGGER_SQL,
    (
        (
            "TYPE authorization_ids IS TABLE OF RAW(16) INDEX BY PLS_INTEGER;",
            "TYPE authorization_ids IS TABLE OF "
            "TB_TA_AGORA_AUTHORIZED_OPEN.SOURCE_AUTHORIZATION_ID%TYPE "
            "INDEX BY PLS_INTEGER;",
        ),
    ),
)


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0014_usage_analytics_schema"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                ACCESS_REQUEST_GUARD_SQL,
                DASHBOARD_TRANSFER_GUARD_SQL,
                REVISION_AUTHORIZATION_GUARD_SQL,
                GRANT_AUTHORIZATION_GUARD_SQL,
                TRANSFER_HISTORY_GUARD_SQL,
                AUTHORIZED_OPEN_GUARD_SQL,
                AUTHORIZED_OPEN_MARK_TRIGGER_SQL,
            ),
            reverse_sql=(
                ORIGINAL_ACCESS_REQUEST_GUARD_SQL,
                ORIGINAL_DASHBOARD_TRANSFER_GUARD_SQL,
                ORIGINAL_REVISION_AUTHORIZATION_GUARD_SQL,
                ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL,
                ORIGINAL_TRANSFER_HISTORY_GUARD_SQL,
                ORIGINAL_AUTHORIZED_OPEN_GUARD_SQL,
                ORIGINAL_AUTHORIZED_OPEN_MARK_TRIGGER_SQL,
            ),
        ),
    ]
