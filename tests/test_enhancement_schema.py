from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from django.apps import apps
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.fields import AddField
from django.db.migrations.operations.models import CreateModel
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState
from django.db.models import Model
from django.utils import timezone

from agora.persistence import migrations as persistence_migrations

_MIGRATION_DIRECTORY = Path(persistence_migrations.__file__).parent
_HISTORICAL_MIGRATION_DIGESTS = {
    "0001_initial_domain.py": "f7ea8af235e026fb322b517269d71302be8dc75309eff2e58e226693d689406f",
    "0002_database_invariants.py": (
        "fee1254b58582b1d10a6d04204b631797fe6d4176f67ef0c73262b6b11732e39"
    ),
    "0003_allow_empty_audit_metadata.py": (
        "01af3517ef7dc9b5465d04f9ff20b0093adedb0bbe9cc6e1de941dabe48bad07"
    ),
    "0004_storage_shard_constraints.py": (
        "4b7905578df01fece7b2eefa4c37799c3372504c0d3e2768e82f02c3b25aa85c"
    ),
    "0005_harden_domain_invariants.py": (
        "4148bfc7824b2c1206a2d2bef45f7035c5bfc42beffffc84db52f64086c77ef7"
    ),
    "0006_durable_storage_ownership.py": (
        "450e53d4d902163c7a752e5275142c207053a92f67bf22219c6f942876e84449"
    ),
    "0007_authentication_controls.py": (
        "8efc789b9403b196b4e9a59041d0af6f7d0a3abb6dcfff3f3fa14d9acc4e2774"
    ),
    "0008_renderauthorization.py": (
        "329968b72dfcef4c5337da937928099c76292154237ac714cbd1e66643b0941c"
    ),
    "0009_rename_tables_to_tb_ta.py": (
        "74328efea32ef3a141cba5cffa4b0df2f75b896d86eee170be6434286eba961a"
    ),
    "0010_apply_agora_project_table_prefix.py": (
        "b1d29813be3fac797ee632a5f7e1dcb7db22b22dd024701b2bcdb8e2d8fcdc9a"
    ),
    "0011_project_viewer_epochs.py": (
        "6f3a934dac8bc26f36c432c6fe57e9b167bbaabd7daea57dce0aae9adecc278e"
    ),
}
_RETIRED_OWNER_EQUALITY_CONSTRAINTS = {
    "AGORA_REV_CREATOR_OWNER_FK",
    "AGORA_GRANT_CREATOR_OWNER_FK",
    "AGORA_GRANT_REVOKER_OWNER_FK",
}
_CORE_MODELS = {
    "DashboardTag": "TB_TA_AGORA_DASHBOARD_TAG",
    "DashboardFavorite": "TB_TA_AGORA_DASH_FAVORITE",
    "DashboardViewerState": "TB_TA_AGORA_DASH_VIEWER_STATE",
    "AccessRequest": "TB_TA_AGORA_ACCESS_REQUEST",
}
_ANALYTICS_MODELS = {
    "AuthorizedOpen",
    "DashboardOpenDaily",
    "DashboardViewerOpenSummary",
    "DashboardOpenSnapshot",
    "AnalyticsPipelineCheckpoint",
}


def _migration(number: int) -> ModuleType:
    matches = sorted(_MIGRATION_DIRECTORY.glob(f"{number:04d}_*.py"))
    assert len(matches) == 1, f"expected one {number:04d} migration, found {matches}"
    return import_module(f"agora.persistence.migrations.{matches[0].stem}")


def _operations(operations: Iterable[Operation]) -> Iterator[Operation]:
    for operation in operations:
        yield operation
        if isinstance(operation, SeparateDatabaseAndState):
            yield from _operations(operation.state_operations)
            yield from _operations(operation.database_operations)


def _migration_operations(module: ModuleType) -> tuple[Operation, ...]:
    migration = cast(Any, module).Migration
    return tuple(_operations(cast(Iterable[Operation], migration.operations)))


def _sql_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return "\n".join(item for item in value if isinstance(item, str))
    return ""


def _migration_sql(module: ModuleType, *, reverse: bool = False) -> str:
    parts = []
    for operation in _migration_operations(module):
        if isinstance(operation, RunSQL):
            parts.append(_sql_value(operation.reverse_sql if reverse else operation.sql))
    return "\n".join(parts).upper()


def _module_source(module: ModuleType) -> str:
    return inspect.getsource(module).upper()


def _model(name: str) -> Any:
    model = apps.get_model("persistence", name)
    assert model is not None
    return model


def _field_names(model: type[Model]) -> set[str]:
    return {field.name for field in model._meta.get_fields()}


def _unique_field_sets(model: type[Model]) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = {
        (field.name,) for field in model._meta.local_fields if field.unique
    }
    unique_sets.update(tuple(fields) for fields in model._meta.unique_together)
    unique_sets.update(
        tuple(constraint.fields)
        for constraint in model._meta.constraints
        if isinstance(constraint, models.UniqueConstraint) and constraint.fields
    )
    return unique_sets


def _index_field_sets(model: type[Model]) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def _constraint_names(model: type[Model], constraint_type: type[models.BaseConstraint]) -> set[str]:
    return {
        constraint.name
        for constraint in model._meta.constraints
        if isinstance(constraint, constraint_type)
    }


def _check_constraint(model: type[Model], name: str) -> models.CheckConstraint:
    for constraint in model._meta.constraints:
        if isinstance(constraint, models.CheckConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"missing check constraint {name}")


def _physical_name(name: str) -> str:
    return connection.ops.quote_name(name).strip('"').upper()


def _column_name(model: type[Model], field_name: str) -> str:
    column = cast(Any, model._meta.get_field(field_name)).column
    assert isinstance(column, str)
    return column.upper()


def _code_source(code: Callable[..., Any] | None) -> str:
    assert code is not None
    return inspect.getsource(code)


def _migration_created_models(module: ModuleType) -> set[str]:
    return {
        operation.name
        for operation in _migration_operations(module)
        if isinstance(operation, CreateModel)
    }


def _migration_added_fields(module: ModuleType, model_name: str) -> set[str]:
    return {
        operation.name
        for operation in _migration_operations(module)
        if isinstance(operation, AddField) and operation.model_name.lower() == model_name.lower()
    }


def test_historical_migrations_are_content_unchanged() -> None:
    actual = {
        name: hashlib.sha256(
            (_MIGRATION_DIRECTORY / name).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for name in _HISTORICAL_MIGRATION_DIGESTS
    }

    assert actual == _HISTORICAL_MIGRATION_DIGESTS


def test_enhancement_migrations_form_one_linear_forward_chain() -> None:
    previous = "0011_project_viewer_epochs"
    for number in (12, 13, 14, 15, 16):
        migration = _migration(number)
        persistence_dependencies = [
            dependency
            for dependency in cast(Any, migration).Migration.dependencies
            if dependency[0] == "persistence"
        ]
        assert persistence_dependencies == [("persistence", previous)]
        previous = migration.__name__.rsplit(".", maxsplit=1)[-1]


def test_oracle_uuid_trigger_variables_follow_their_varchar_columns() -> None:
    migration = _migration(15)
    forward_sql = _migration_sql(migration)

    assert "RAW(16)" not in forward_sql
    for column in (
        "TB_TA_AGORA_DASHBOARD.OWNER_ID%TYPE",
        "TB_TA_AGORA_DASH_TRANSFER.DASHBOARD_ID%TYPE",
        "TB_TA_AGORA_RENDER_AUTHORIZATION.DASHBOARD_ID%TYPE",
        "TB_TA_AGORA_AUTHORIZED_OPEN.SOURCE_AUTHORIZATION_ID%TYPE",
    ):
        assert column in forward_sql


def test_grant_trigger_rejects_an_initial_non_owner_revoker() -> None:
    forward_sql = _migration_sql(_migration(16))

    assert ":NEW.REVOKED_BY_ID IS NOT NULL" in forward_sql
    assert ":NEW.REVOKED_BY_ID <> CURRENT_OWNER" in forward_sql
    assert "GRANT REVOKER MUST BE THE CURRENT OWNER" in forward_sql


def test_each_enhancement_schema_is_introduced_in_its_approved_migration() -> None:
    migration_12 = _migration(12)
    migration_13 = _migration(13)
    migration_14 = _migration(14)

    assert set(_CORE_MODELS) <= _migration_created_models(migration_12)
    assert {
        "publication_version",
        "last_published_at",
        "publication_note",
        "data_as_of",
        "freshness_interval_seconds",
        "freshness_confirmed_at",
        "stale_after",
    } <= _migration_added_fields(migration_12, "dashboard")
    assert "DashboardOwnershipTransfer" in _migration_created_models(migration_13)
    assert "last_ownership_transfer" in _migration_added_fields(migration_13, "dashboard")
    assert "owner_transfer_epoch" in _migration_added_fields(migration_13, "renderauthorization")
    assert _ANALYTICS_MODELS <= _migration_created_models(migration_14)
    assert {"publication_version", "authorized_open_captured_at"} <= _migration_added_fields(
        migration_14, "renderauthorization"
    )


def test_core_reversal_restores_a_pre_enhancement_dashboard_guard_last() -> None:
    migration = _migration(12)
    reverse_statements = cast(tuple[str, ...], cast(Any, migration).DROP_CORE_GUARDS_SQL)
    restored_guard = reverse_statements[-1].strip().upper()

    assert restored_guard.startswith("CREATE OR REPLACE TRIGGER AGORA_DASHBOARD_GUARD")
    assert re.search(
        r"BEFORE\s+INSERT\s+OR\s+UPDATE\s+OR\s+DELETE\s+ON\s+"
        r"TB_TA_AGORA_DASHBOARD\b",
        restored_guard,
    )
    removed_columns = {
        "PUBLICATION_VERSION",
        "LAST_PUBLISHED_AT",
        "PUBLICATION_NOTE",
        "DATA_AS_OF",
        "FRESHNESS_INTERVAL_SECONDS",
        "FRESHNESS_CONFIRMED_AT",
        "STALE_AFTER",
        "LAST_OWNERSHIP_TRANSFER_ID",
    }
    assert all(column not in restored_guard for column in removed_columns)


def test_core_models_express_deduplication_limits_and_bounded_read_indexes() -> None:
    tag = _model("DashboardTag")
    favorite = _model("DashboardFavorite")
    viewer_state = _model("DashboardViewerState")
    access_request = _model("AccessRequest")
    dashboard = _model("Dashboard")
    viewer_grant = _model("ViewerGrant")

    for name, table in _CORE_MODELS.items():
        assert _model(name)._meta.db_table == table
    assert {"dashboard", "label", "key", "slot"} <= _field_names(tag)
    assert {("dashboard", "key"), ("dashboard", "slot")} <= _unique_field_sets(tag)
    assert ("key", "dashboard") in _index_field_sets(tag)
    assert {"agora_tag_slot_range", "agora_tag_values_not_empty"} <= _constraint_names(
        tag, models.CheckConstraint
    )

    assert {"user", "dashboard", "created_at"} <= _field_names(favorite)
    assert ("user", "dashboard") in _unique_field_sets(favorite)
    assert ("user", "-created_at", "-id") in _index_field_sets(favorite)

    assert {
        "user",
        "dashboard",
        "last_viewed_at",
        "seen_publication_version",
    } <= _field_names(viewer_state)
    assert ("user", "dashboard") in _unique_field_sets(viewer_state)
    assert ("user", "-last_viewed_at", "-id") in _index_field_sets(viewer_state)

    assert {
        "dashboard",
        "requester",
        "status",
        "message",
        "requested_at",
        "resolved_at",
        "resolved_by",
    } <= _field_names(access_request)
    assert ("dashboard", "requester") in _unique_field_sets(access_request)
    assert (
        "dashboard",
        "status",
        "-requested_at",
        "-id",
    ) in _index_field_sets(access_request)
    assert {"agora_access_status_valid", "agora_access_resolution_match"} <= _constraint_names(
        access_request, models.CheckConstraint
    )

    dashboard_fields = _field_names(dashboard)
    assert {
        "publication_version",
        "last_published_at",
        "publication_note",
        "data_as_of",
        "freshness_interval_seconds",
        "freshness_confirmed_at",
        "stale_after",
    } <= dashboard_fields
    assert "is_stale" not in dashboard_fields
    assert ("owner", "stale_after", "id") in _index_field_sets(dashboard)
    assert ("stale_after", "id") in _index_field_sets(dashboard)
    assert (
        "viewer",
        "revoked_at",
        "-created_at",
        "-id",
    ) in _index_field_sets(viewer_grant)
    expected_freshness_condition = models.Q(
        freshness_interval_seconds__isnull=True,
        freshness_confirmed_at__isnull=True,
        stale_after__isnull=True,
    ) | models.Q(
        freshness_interval_seconds__isnull=False,
        freshness_interval_seconds__gt=0,
        freshness_interval_seconds__lte=31_536_000,
        freshness_confirmed_at__isnull=False,
        stale_after__isnull=False,
    )
    assert (
        _check_constraint(dashboard, "agora_dash_fresh_fields_match").condition
        == expected_freshness_condition
    )
    assert "FRESHNESS_INTERVAL_SECONDS__ISNULL=FALSE" in _module_source(_migration(12))
    publication_note = dashboard._meta.get_field("publication_note")
    assert isinstance(publication_note, models.CharField)
    assert publication_note.max_length == 240


def test_access_request_queue_interface_is_dashboard_scoped_and_index_streamable() -> None:
    query_module = import_module("agora.persistence.enhancement_queries")
    pending_access_requests = cast(Any, query_module).pending_access_requests
    parameters = inspect.signature(pending_access_requests).parameters

    assert tuple(parameters) == ("owner_id", "dashboard_id", "limit", "before")
    source = inspect.getsource(pending_access_requests)
    assert "dashboard_id=dashboard_id" in source


@pytest.mark.django_db(transaction=True)
def test_tag_slots_and_keys_enforce_the_effective_five_tag_ceiling() -> None:
    user = _model("User").objects.create_user("SCHEMA.TAG.OWNER")
    dashboard = _model("Dashboard").objects.create(owner=user, name="Tags")
    tag = _model("DashboardTag")
    tag.objects.bulk_create(
        [
            tag(dashboard=dashboard, label=f"Tag {slot}", key=f"tag {slot}", slot=slot)
            for slot in range(1, 6)
        ]
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        tag.objects.bulk_create([tag(dashboard=dashboard, label="Six", key="six", slot=6)])
    with pytest.raises(IntegrityError), transaction.atomic():
        tag.objects.bulk_create([tag(dashboard=dashboard, label="Again", key="tag 1", slot=5)])


@pytest.mark.django_db(transaction=True)
def test_core_singleton_rows_and_access_lifecycle_are_database_enforced() -> None:
    user_model = _model("User")
    dashboard_model = _model("Dashboard")
    favorite = _model("DashboardFavorite")
    viewer_state = _model("DashboardViewerState")
    access_request = _model("AccessRequest")
    owner = user_model.objects.create_user("SCHEMA.CORE.OWNER")
    requester = user_model.objects.create_user("SCHEMA.CORE.REQUESTER")
    dashboard = dashboard_model.objects.create(owner=owner, name="Core")
    revision = _model("Revision").objects.create(
        dashboard=dashboard,
        number=1,
        created_by=owner,
    )
    published_at = timezone.now()
    dashboard.latest_revision = revision
    dashboard.published_revision = revision
    dashboard.first_published_at = published_at
    dashboard.publication_version = 1
    dashboard.last_published_at = published_at
    dashboard.state = "published"
    dashboard.save()

    favorite.objects.create(user=requester, dashboard=dashboard)
    with pytest.raises(IntegrityError), transaction.atomic():
        favorite.objects.bulk_create([favorite(user=requester, dashboard=dashboard)])

    viewer_state.objects.create(
        user=requester,
        dashboard=dashboard,
        last_viewed_at=timezone.now(),
        seen_publication_version=1,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        viewer_state.objects.bulk_create(
            [
                viewer_state(
                    user=requester,
                    dashboard=dashboard,
                    last_viewed_at=timezone.now(),
                    seen_publication_version=1,
                )
            ]
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        viewer_state.objects.filter(user=requester, dashboard=dashboard).update(
            seen_publication_version=0
        )
    unseen_user = user_model.objects.create_user("SCHEMA.CORE.UNSEEN")
    with pytest.raises(DatabaseError), transaction.atomic():
        viewer_state.objects.bulk_create(
            [
                viewer_state(
                    user=unseen_user,
                    dashboard=dashboard,
                    last_viewed_at=timezone.now(),
                    seen_publication_version=2,
                )
            ]
        )

    access_request.objects.create(dashboard=dashboard, requester=requester)
    with pytest.raises(IntegrityError), transaction.atomic():
        access_request.objects.bulk_create(
            [access_request(dashboard=dashboard, requester=requester)]
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        access_request.objects.bulk_create(
            [
                access_request(
                    dashboard=dashboard,
                    requester=owner,
                    status="approved",
                )
            ]
        )
    draft_dashboard = dashboard_model.objects.create(owner=owner, name="Not requestable")
    with pytest.raises(DatabaseError), transaction.atomic():
        access_request.objects.bulk_create(
            [access_request(dashboard=draft_dashboard, requester=unseen_user)]
        )


def test_transfer_migration_replaces_owner_equality_with_current_owner_guards() -> None:
    migration = _migration(13)
    forward_sql = _migration_sql(migration)
    source = _module_source(migration)
    revision_guard_sql = cast(str, cast(Any, migration).REVISION_AUTHORIZATION_GUARD_SQL).upper()

    for constraint in _RETIRED_OWNER_EQUALITY_CONSTRAINTS:
        assert f"DROP CONSTRAINT {constraint}" in forward_sql
    for trigger in (
        "AGORA_DASHBOARD_GUARD",
        "AGORA_REVISION_AUTH_GUARD",
        "AGORA_GRANT_IMMUT_GUARD",
    ):
        assert re.search(rf"CREATE\s+OR\s+REPLACE\s+TRIGGER\s+{trigger}\b", forward_sql)
    assert "LAST_OWNERSHIP_TRANSFER_ID" in forward_sql
    assert "PREVIOUS_TRANSFER_ID" in forward_sql
    assert "CREATE UNIQUE INDEX" in forward_sql and "NVL" in forward_sql
    assert "CREATED_BY_ID" in forward_sql and "OWNER_ID" in forward_sql
    assert "REVOKED_BY_ID" in forward_sql
    assert "FOR UPDATE" in forward_sql
    assert revision_guard_sql.index("FROM TB_TA_AGORA_DASHBOARD") < (
        revision_guard_sql.index("FROM TB_TA_AGORA_USER")
    ), "revision and transfer writes must share the Dashboard-before-User lock order"
    assert "REVOKED_AT IS NULL" in forward_sql
    assert "TB_TA_AGORA_ACCESS_REQUEST" in forward_sql
    assert re.search(r"STATUS\s*=\s*'PENDING'", forward_sql)

    transfer_model = _model("DashboardOwnershipTransfer")
    assert transfer_model._meta.db_table == "TB_TA_AGORA_DASH_TRANSFER"
    assert {"dashboard", "from_owner", "to_owner", "previous_transfer", "transferred_at"} <= (
        _field_names(transfer_model)
    )
    dashboard_transfer = _model("Dashboard")._meta.get_field("last_ownership_transfer")
    assert isinstance(dashboard_transfer, models.OneToOneField)
    assert dashboard_transfer.remote_field.model is transfer_model
    render_authorization = _model("RenderAuthorization")
    owner_epoch = render_authorization._meta.get_field("owner_transfer_epoch")
    assert isinstance(owner_epoch, models.ForeignKey)
    assert owner_epoch.null is True
    assert owner_epoch.remote_field.model is transfer_model
    assert owner_epoch.remote_field.on_delete is models.PROTECT
    expected_epoch_grant_constraint = models.Q(owner_transfer_epoch__isnull=True) | models.Q(
        viewer_grant__isnull=True
    )
    assert any(
        isinstance(constraint, models.CheckConstraint)
        and constraint.name == "agora_render_epoch_grant_xor"
        and constraint.condition == expected_epoch_grant_constraint
        for constraint in render_authorization._meta.constraints
    )
    assert "DASHBOARDOWNERSHIPTRANSFER" in source


def test_transfer_related_mutations_use_dashboard_before_user_lock_order() -> None:
    access_module = import_module("agora.persistence.access")
    services_module = import_module("agora.persistence.services")
    guarded_functions = (
        cast(Any, access_module)._lock_active_owner,
        cast(Any, services_module)._commit_revision_metadata,
    )

    for function in guarded_functions:
        source = inspect.getsource(function).upper()
        assert source.index("DASHBOARD.OBJECTS.SELECT_FOR_UPDATE") < source.index(
            "USER.OBJECTS.SELECT_FOR_UPDATE"
        ), f"{function.__name__} must lock Dashboard before User"


def test_transfer_reversal_fails_safe_without_rewriting_retained_history() -> None:
    migration = _migration(13)
    top_level_operations = cast(list[Operation], cast(Any, migration).Migration.operations)
    operations = _migration_operations(migration)
    reversal_guards = [
        operation
        for operation in operations
        if isinstance(operation, RunPython)
        and operation.reverse_code is not RunPython.noop
        and "TRANSFER" in _code_source(operation.reverse_code).upper()
    ]

    assert reversal_guards, "0013 needs a reverse-first preflight for real transfer history"
    assert top_level_operations[-1] in reversal_guards
    guard_source = "\n".join(_code_source(operation.reverse_code) for operation in reversal_guards)
    assert "RuntimeError" in guard_source
    assert "DashboardOwnershipTransfer" in guard_source or "DASH_TRANSFER" in guard_source
    reverse_sql = _migration_sql(migration, reverse=True)
    assert "NEW DASHBOARDS MUST BEGIN AS PRIVATE DRAFTS" in reverse_sql
    assert "DELETED DASHBOARDS ARE TERMINAL TOMBSTONES" in reverse_sql
    assert "DASHBOARD LIFECYCLE TRANSITION IS NOT ALLOWED" in reverse_sql
    assert "ARCHIVED DASHBOARDS ARE READ-ONLY" in reverse_sql
    for table in (
        "TB_TA_AGORA_REVISION",
        "TB_TA_AGORA_VIEWER_GRANT",
        "TB_TA_AGORA_AUDIT_EVENT",
        "TB_TA_AGORA_DASH_TRANSFER",
    ):
        assert re.search(rf"\b(?:UPDATE|DELETE\s+FROM)\s+{table}\b", reverse_sql) is None


def test_analytics_models_are_compact_deduplicated_and_indexed_for_batches() -> None:
    authorized_open = _model("AuthorizedOpen")
    daily = _model("DashboardOpenDaily")
    viewer_summary = _model("DashboardViewerOpenSummary")
    snapshot = _model("DashboardOpenSnapshot")
    checkpoint = _model("AnalyticsPipelineCheckpoint")
    analytics_models = (authorized_open, daily, viewer_summary, snapshot, checkpoint)

    for model in analytics_models:
        assert model._meta.db_table.startswith("TB_TA_AGORA_")
        assert len(model._meta.db_table.encode("ascii")) <= 128
        for index in model._meta.indexes:
            assert len(index.name.encode("ascii")) <= 30

    raw_fields = _field_names(authorized_open)
    assert {
        "source_authorization",
        "dashboard",
        "viewer",
        "revision",
        "publication_version",
        "opened_at",
        "aggregated_at",
    } <= raw_fields
    assert ("source_authorization",) in _unique_field_sets(authorized_open)
    assert {
        ("opened_at", "id"),
        ("aggregated_at", "opened_at", "id"),
    } <= _index_field_sets(authorized_open)

    assert {"dashboard", "day", "authorized_open_count"} <= _field_names(daily)
    assert ("dashboard", "day") in _unique_field_sets(daily)
    assert ("dashboard", "-day", "-id") in _index_field_sets(daily)
    assert {
        "dashboard",
        "viewer",
        "authorized_open_count",
        "first_opened_at",
        "last_opened_at",
    } <= _field_names(viewer_summary)
    assert ("dashboard", "viewer") in _unique_field_sets(viewer_summary)
    assert {
        "dashboard",
        "authorized_open_count",
        "last_opened_at",
        "captured_through_open_id",
        "updated_at",
    } <= _field_names(snapshot)
    assert ("dashboard",) in _unique_field_sets(snapshot)
    assert {"pipeline_key", "last_completed_open_id", "updated_at"} <= _field_names(checkpoint)
    assert ("pipeline_key",) in _unique_field_sets(checkpoint)
    assert any(
        "authorized_opens_v1" in str(constraint.condition)
        for constraint in checkpoint._meta.constraints
        if isinstance(constraint, models.CheckConstraint)
    )

    assert "authorized_open_captured_at" in _field_names(_model("RenderAuthorization"))
    expected_render_publication_condition = models.Q(
        audience="preview", publication_version__isnull=True
    ) | models.Q(
        audience="viewer",
        publication_version__isnull=False,
        publication_version__gt=0,
    )
    assert (
        _check_constraint(_model("RenderAuthorization"), "agora_render_pub_version_match").condition
        == expected_render_publication_condition
    )
    forbidden = re.compile(
        r"^(?:ip(?:_address)?|user_agent|referrer|click(?:_.+)?|scroll(?:_.+)?|"
        r"filter(?:_.+)?|iframe(?:_.+)?)$",
        re.IGNORECASE,
    )
    for model in analytics_models:
        assert not {field for field in _field_names(model) if forbidden.search(field)}
    dashboard_fields = _field_names(_model("Dashboard"))
    assert {"open_count", "popularity", "popularity_score"}.isdisjoint(dashboard_fields)


def test_analytics_migration_defines_idempotent_viewer_only_raw_capture() -> None:
    migration = _migration(14)
    source = _module_source(migration)
    forward_sql = _migration_sql(migration)
    open_guard_sql = cast(str, cast(Any, migration).AUTHORIZED_OPEN_GUARD_SQL).upper()
    data_migration_source = "\n".join(
        _code_source(operation.code).upper()
        for operation in _migration_operations(migration)
        if isinstance(operation, RunPython) and operation.code is not RunPython.noop
    )

    assert "RENDERAUTHORIZATION" in source.upper()
    assert "AUTHORIZEDOPEN" in source
    assert "AUDIENCE" in forward_sql
    assert "VIEWER" in forward_sql
    assert "AUTHORIZED_OPEN_CAPTURED_AT" in forward_sql
    assert "AUTHORIZATION.OWNER_TRANSFER_EPOCH_ID" in open_guard_sql
    assert "LAST_OWNERSHIP_TRANSFER_ID" in open_guard_sql
    assert "SOURCE_PUBLICATION_VERSION IS NULL" in open_guard_sql
    assert "PUBLICATION_VERSION__ISNULL=FALSE" in source
    assert re.search(
        r"SOURCE_OWNER_EPOCH\s+IS\s+NULL\s+AND\s+"
        r"DASHBOARD_OWNER_EPOCH\s+IS\s+NOT\s+NULL",
        open_guard_sql,
    )
    assert re.search(
        r"SOURCE_OWNER_EPOCH\s+IS\s+NOT\s+NULL\s+AND\s+"
        r"DASHBOARD_OWNER_EPOCH\s+IS\s+NULL",
        open_guard_sql,
    )
    assert re.search(
        r"SOURCE_OWNER_EPOCH\s*<>\s*DASHBOARD_OWNER_EPOCH",
        open_guard_sql,
    )
    assert re.search(
        r"SOURCE_GRANT\s+IS\s+NULL\s+OR\s+SOURCE_OWNER_EPOCH\s+IS\s+NOT\s+NULL",
        open_guard_sql,
    )
    assert "AGGREGATED_AT IS NULL" in forward_sql
    assert "NUMTODSINTERVAL(90, 'DAY')" in forward_sql
    assert "LAST_COMPLETED_OPEN_ID" in forward_sql
    assert "RAISE_APPLICATION_ERROR" in forward_sql
    assert re.search(
        r"BEFORE\s+(?:UPDATE\s+OR\s+DELETE|DELETE\s+OR\s+UPDATE)\s+ON\s+"
        r"TB_TA_AGORA_ANALYTICS_CKPT",
        forward_sql,
    )
    assert re.search(
        r":NEW\.LAST_COMPLETED_OPEN_ID\s*<\s*:OLD\.LAST_COMPLETED_OPEN_ID",
        forward_sql,
    )
    assert "AUTHORIZED_OPEN_CAPTURED_AT" in data_migration_source
    assert "CREATED_AT" in data_migration_source


def test_analytics_jobs_lock_only_an_outer_bounded_oracle_candidate_set() -> None:
    analytics = import_module("agora.persistence.analytics")
    aggregation_source = inspect.getsource(cast(Any, analytics).aggregate_authorized_opens)
    candidate_slice = aggregation_source.index('.values("id")[:bounded]')
    outer_lock = aggregation_source.index("_AuthorizedOpen.objects.select_for_update()")
    locked_query_end = aggregation_source.index("if not opens:")

    assert candidate_slice < outer_lock
    assert "Subquery(candidate_open_ids)" in aggregation_source[outer_lock:locked_query_end]
    assert "[:bounded]" not in aggregation_source[outer_lock:locked_query_end]

    purge_source = inspect.getsource(cast(Any, analytics).purge_authorized_opens)
    purge_candidates = purge_source[
        purge_source.index("candidate_open_ids =") : purge_source.index("removable_ids =")
    ]
    purge_filters = purge_source[
        purge_source.index("removable_ids =") : purge_source.index("if not removable_ids:")
    ]
    assert "opened_at__lt=before" in purge_candidates
    assert '.order_by("opened_at", "id")' in purge_candidates
    assert '.values("id")[:bounded]' in purge_candidates
    assert "Subquery(candidate_open_ids)" in purge_filters
    assert "aggregated_at__isnull=False" in purge_filters
    assert "id__lte=checkpoint.last_completed_open_id" in purge_filters


def test_new_oracle_identifiers_stay_within_supported_exact_name_limits() -> None:
    model_names = (
        *_CORE_MODELS,
        "Dashboard",
        "DashboardOwnershipTransfer",
        "RenderAuthorization",
        *_ANALYTICS_MODELS,
    )
    for model in (_model(name) for name in model_names):
        assert model._meta.db_table.startswith("TB_TA_AGORA_")
        assert len(model._meta.db_table.encode("ascii")) <= 128
        for index in model._meta.indexes:
            assert len(index.name.encode("ascii")) <= 30
        for constraint in model._meta.constraints:
            assert len(_physical_name(constraint.name).encode("ascii")) <= 30

    sql_identifier_pattern = re.compile(
        r"(?:CONSTRAINT|INDEX|TRIGGER)\s+(AGORA_[A-Z0-9_]+)",
        re.IGNORECASE,
    )
    for migration_number in (12, 13, 14):
        identifiers = sql_identifier_pattern.findall(_module_source(_migration(migration_number)))
        assert identifiers
        assert all(len(identifier.encode("ascii")) <= 30 for identifier in identifiers)


def test_published_view_uses_one_raw_source_and_never_updates_rollups() -> None:
    authorization = import_module("agora.rendering.authorization")
    published_source = inspect.getsource(cast(Any, authorization).issue_published_view)
    helper_source = inspect.getsource(cast(Any, authorization)._create_authorization)
    request_path_source = f"{published_source}\n{helper_source}"

    assert "dashboard.view_started" not in request_path_source
    assert (
        "AuthorizedOpen" in request_path_source or "capture_authorized_open" in request_path_source
    )
    for aggregate_model in _ANALYTICS_MODELS - {"AuthorizedOpen"}:
        assert aggregate_model not in request_path_source


def test_portal_code_has_no_raw_authorized_open_query_surface() -> None:
    portal_directory = Path(cast(str, import_module("agora.portal").__file__)).parent
    portal_source = "\n".join(
        path.read_text(encoding="utf-8") for path in portal_directory.rglob("*.py")
    )

    assert re.search(r"\bAuthorizedOpen\b", portal_source) is None
    assert "TB_TA_AGORA_AUTHORIZED_OPEN" not in portal_source


@pytest.mark.django_db(transaction=True)
def test_new_oracle_objects_are_installed_exactly_and_compile_cleanly() -> None:
    assert connection.vendor == "oracle"
    model_names = (*_CORE_MODELS, "DashboardOwnershipTransfer", *_ANALYTICS_MODELS)
    expected_tables = {_model(name)._meta.db_table for name in model_names}
    expected_indexes = {
        _physical_name(index.name): (
            model._meta.db_table,
            [_column_name(model, field.lstrip("-")) for field in index.fields],
        )
        for model in (_model(name) for name in model_names)
        for index in model._meta.indexes
    }
    trigger_pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+(?:COMPOUND\s+)?TRIGGER\s+([A-Z0-9_]+)",
        re.IGNORECASE,
    )
    expected_triggers = {
        match.upper()
        for migration_number in (12, 13, 14)
        for match in trigger_pattern.findall(_module_source(_migration(migration_number)))
    }

    with connection.cursor() as cursor:
        cursor.execute("SELECT table_name FROM user_tables")
        tables = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT constraint_name FROM user_constraints")
        constraints = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT index_name, table_name FROM user_indexes")
        indexes = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT index_name, column_position, column_name "
            "FROM user_ind_columns ORDER BY index_name, column_position"
        )
        index_columns: dict[str, list[str]] = {}
        for index_name, _position, column_name in cursor.fetchall():
            index_columns.setdefault(index_name, []).append(column_name)
        cursor.execute(
            "SELECT index_name, column_position, column_expression "
            "FROM user_ind_expressions ORDER BY index_name, column_position"
        )
        index_expressions = {
            (index_name, position): str(expression).upper()
            for index_name, position, expression in cursor.fetchall()
        }
        cursor.execute("SELECT trigger_name FROM user_triggers")
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT name, type, line, position, text FROM user_errors "
            "WHERE name LIKE 'AGORA%' ORDER BY name, sequence"
        )
        compile_errors = cursor.fetchall()

    assert expected_tables <= tables
    assert _RETIRED_OWNER_EQUALITY_CONSTRAINTS.isdisjoint(constraints)
    for index_name, (table_name, columns) in expected_indexes.items():
        assert indexes[index_name] == table_name
        assert len(index_columns[index_name]) == len(columns)
        for position, (actual_column, expected_column) in enumerate(
            zip(index_columns[index_name], columns, strict=True),
            start=1,
        ):
            assert actual_column == expected_column or expected_column in index_expressions.get(
                (index_name, position), ""
            )
    assert expected_triggers <= triggers
    assert compile_errors == []


@pytest.mark.django_db(transaction=True)
def test_freshness_derivation_cannot_be_corrupted_by_bulk_sql() -> None:
    user = _model("User").objects.create_user("SCHEMA.FRESH.OWNER")
    dashboard_model = _model("Dashboard")
    dashboard = dashboard_model.objects.create(owner=user, name="Freshness")
    confirmed_at = timezone.now().replace(microsecond=0)
    expected_stale_after = confirmed_at + timedelta(seconds=60)

    with pytest.raises((IntegrityError, DatabaseError)), transaction.atomic():
        dashboard_model.objects.filter(id=dashboard.pk).update(
            freshness_interval_seconds=60,
            freshness_confirmed_at=confirmed_at,
            stale_after=None,
        )

    dashboard_model.objects.filter(id=dashboard.pk).update(
        freshness_interval_seconds=60,
        freshness_confirmed_at=confirmed_at,
        stale_after=expected_stale_after,
    )
    dashboard.refresh_from_db()
    assert dashboard.stale_after == expected_stale_after

    wrong_stale_after = expected_stale_after + timedelta(seconds=1)
    try:
        with transaction.atomic():
            dashboard_model.objects.filter(id=dashboard.pk).update(stale_after=wrong_stale_after)
    except IntegrityError, DatabaseError:
        pass
    else:
        dashboard.refresh_from_db()
        assert dashboard.stale_after == expected_stale_after

    with pytest.raises((IntegrityError, DatabaseError)), transaction.atomic():
        dashboard_model.objects.filter(id=dashboard.pk).update(
            freshness_interval_seconds=31_536_001,
            stale_after=confirmed_at + timedelta(seconds=31_536_001),
        )
