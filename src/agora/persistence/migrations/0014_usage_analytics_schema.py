"""Add the single authorized-open metric and bounded asynchronous rollups."""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F

AUTHORIZED_OPEN_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_AUTH_OPEN_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_AUTHORIZED_OPEN
FOR EACH ROW
DECLARE
    source_audience NVARCHAR2(8);
    source_dashboard RAW(16);
    source_viewer RAW(16);
    source_revision RAW(16);
    source_grant RAW(16);
    source_owner_epoch RAW(16);
    source_publication_version NUMBER(19);
    source_created_at TIMESTAMP WITH TIME ZONE;
    source_captured_at TIMESTAMP WITH TIME ZONE;
    source_revoked_at TIMESTAMP WITH TIME ZONE;
    source_viewer_auth_version NUMBER(19);
    viewer_active NUMBER(1);
    viewer_auth_version NUMBER(19);
    dashboard_owner RAW(16);
    dashboard_owner_epoch RAW(16);
    dashboard_state NVARCHAR2(16);
    dashboard_revision RAW(16);
    dashboard_publication_version NUMBER(19);
    valid_grants NUMBER;
    checkpoint_open_id NUMBER(19);
BEGIN
    IF INSERTING THEN
        SELECT authorization.audience,
               authorization.dashboard_id,
               authorization.viewer_id,
               authorization.revision_id,
               authorization.viewer_grant_id,
               authorization.owner_transfer_epoch_id,
               authorization.publication_version,
               authorization.created_at,
               authorization.authorized_open_captured_at,
               authorization.revoked_at,
               authorization.viewer_auth_version,
               viewer.is_active,
               viewer.auth_version
        INTO source_audience,
             source_dashboard,
             source_viewer,
             source_revision,
             source_grant,
             source_owner_epoch,
             source_publication_version,
             source_created_at,
             source_captured_at,
             source_revoked_at,
             source_viewer_auth_version,
             viewer_active,
             viewer_auth_version
        FROM TB_TA_AGORA_RENDER_AUTHORIZATION authorization
        JOIN TB_TA_AGORA_USER viewer ON viewer.id = authorization.viewer_id
        WHERE authorization.id = :NEW.source_authorization_id
        FOR UPDATE OF authorization.authorized_open_captured_at;

        SELECT owner_id,
               last_ownership_transfer_id,
               state,
               published_revision_id,
               publication_version
        INTO dashboard_owner,
             dashboard_owner_epoch,
             dashboard_state,
             dashboard_revision,
             dashboard_publication_version
        FROM TB_TA_AGORA_DASHBOARD
        WHERE id = source_dashboard;

        IF source_audience <> 'viewer'
           OR source_captured_at IS NOT NULL
           OR source_revoked_at IS NOT NULL
           OR viewer_active <> 1
           OR source_viewer_auth_version <> viewer_auth_version
           OR source_publication_version IS NULL
           OR dashboard_state <> 'published'
           OR dashboard_revision <> source_revision
           OR dashboard_publication_version <> source_publication_version
           OR :NEW.dashboard_id <> source_dashboard
           OR :NEW.viewer_id <> source_viewer
           OR :NEW.revision_id <> source_revision
           OR :NEW.publication_version <> source_publication_version
           OR :NEW.opened_at <> source_created_at
           OR :NEW.aggregated_at IS NOT NULL THEN
            raise_application_error(-20025, 'authorized-open source is invalid');
        END IF;

        IF source_viewer <> dashboard_owner THEN
            IF source_grant IS NULL OR source_owner_epoch IS NOT NULL THEN
                raise_application_error(-20025, 'authorized-open viewer grant is missing');
            END IF;
            SELECT COUNT(*)
            INTO valid_grants
            FROM TB_TA_AGORA_VIEWER_GRANT
            WHERE id = source_grant
              AND dashboard_id = source_dashboard
              AND viewer_id = source_viewer
              AND revoked_at IS NULL;
            IF valid_grants <> 1 THEN
                raise_application_error(-20025, 'authorized-open viewer grant is invalid');
            END IF;
        ELSIF source_grant IS NOT NULL
           OR (source_owner_epoch IS NULL AND dashboard_owner_epoch IS NOT NULL)
           OR (source_owner_epoch IS NOT NULL AND dashboard_owner_epoch IS NULL)
           OR source_owner_epoch <> dashboard_owner_epoch THEN
            raise_application_error(-20025, 'owner authorized-open cannot bind a grant');
        END IF;
        RETURN;
    END IF;

    IF UPDATING THEN
        IF :NEW.id <> :OLD.id
           OR :NEW.source_authorization_id <> :OLD.source_authorization_id
           OR :NEW.dashboard_id <> :OLD.dashboard_id
           OR :NEW.viewer_id <> :OLD.viewer_id
           OR :NEW.revision_id <> :OLD.revision_id
           OR :NEW.publication_version <> :OLD.publication_version
           OR :NEW.opened_at <> :OLD.opened_at THEN
            raise_application_error(-20025, 'authorized-open source fields are immutable');
        END IF;
        IF :OLD.aggregated_at IS NOT NULL
           AND (
               :NEW.aggregated_at IS NULL
               OR :NEW.aggregated_at <> :OLD.aggregated_at
           ) THEN
            raise_application_error(-20025, 'authorized-open aggregation is one-way');
        END IF;
        RETURN;
    END IF;

    IF :OLD.aggregated_at IS NULL
       OR :OLD.opened_at > SYSTIMESTAMP - NUMTODSINTERVAL(90, 'DAY') THEN
        raise_application_error(-20025, 'authorized-open is not retention eligible');
    END IF;
    SELECT last_completed_open_id
    INTO checkpoint_open_id
    FROM TB_TA_AGORA_ANALYTICS_CKPT
    WHERE pipeline_key = 'authorized_opens_v1';
    IF :OLD.id > checkpoint_open_id THEN
        raise_application_error(-20025, 'authorized-open is ahead of checkpoint');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20025, 'authorized-open dependency is missing');
END;
"""


AUTHORIZED_OPEN_MARK_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_AUTH_OPEN_MARK
FOR INSERT ON TB_TA_AGORA_AUTHORIZED_OPEN
COMPOUND TRIGGER
    TYPE authorization_ids IS TABLE OF RAW(16) INDEX BY PLS_INTEGER;
    TYPE opened_times IS TABLE OF TIMESTAMP WITH TIME ZONE INDEX BY PLS_INTEGER;
    pending_authorizations authorization_ids;
    pending_times opened_times;
    pending_count PLS_INTEGER := 0;

    AFTER EACH ROW IS
    BEGIN
        pending_count := pending_count + 1;
        pending_authorizations(pending_count) := :NEW.source_authorization_id;
        pending_times(pending_count) := :NEW.opened_at;
    END AFTER EACH ROW;

    AFTER STATEMENT IS
    BEGIN
        FOR position IN 1 .. pending_count LOOP
            UPDATE TB_TA_AGORA_RENDER_AUTHORIZATION
            SET authorized_open_captured_at = pending_times(position)
            WHERE id = pending_authorizations(position)
              AND authorized_open_captured_at IS NULL;
            IF SQL%ROWCOUNT <> 1 THEN
                raise_application_error(-20026, 'authorized-open source was already captured');
            END IF;
        END LOOP;
    END AFTER STATEMENT;
END;
"""


RENDER_CAPTURE_MARKER_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_RENDER_OPEN_GUARD
BEFORE UPDATE OF authorized_open_captured_at ON TB_TA_AGORA_RENDER_AUTHORIZATION
FOR EACH ROW
DECLARE
    matching_open NUMBER;
BEGIN
    IF (:NEW.authorized_open_captured_at IS NULL AND :OLD.authorized_open_captured_at IS NULL)
       OR :NEW.authorized_open_captured_at = :OLD.authorized_open_captured_at THEN
        RETURN;
    END IF;
    IF :OLD.authorized_open_captured_at IS NOT NULL
       OR :NEW.authorized_open_captured_at IS NULL
       OR :NEW.audience <> 'viewer'
       OR :NEW.authorized_open_captured_at <> :NEW.created_at THEN
        raise_application_error(-20027, 'render capture marker is immutable');
    END IF;
    SELECT COUNT(*)
    INTO matching_open
    FROM TB_TA_AGORA_AUTHORIZED_OPEN
    WHERE source_authorization_id = :NEW.id
      AND opened_at = :NEW.authorized_open_captured_at;
    IF matching_open <> 1 THEN
        raise_application_error(-20027, 'render capture marker lacks its source row');
    END IF;
END;
"""


ANALYTICS_CHECKPOINT_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_ANALYTICS_CKPT_GUARD
BEFORE UPDATE OR DELETE ON TB_TA_AGORA_ANALYTICS_CKPT
FOR EACH ROW
BEGIN
    IF DELETING THEN
        raise_application_error(-20028, 'analytics checkpoint is retained');
    END IF;
    IF :NEW.pipeline_key <> :OLD.pipeline_key
       OR :NEW.last_completed_open_id < :OLD.last_completed_open_id THEN
        raise_application_error(-20028, 'analytics checkpoint cannot move backward');
    END IF;
END;
"""


DROP_ANALYTICS_GUARDS_SQL = (
    "DROP TRIGGER AGORA_ANALYTICS_CKPT_GUARD",
    "DROP TRIGGER AGORA_RENDER_OPEN_GUARD",
    "DROP TRIGGER AGORA_AUTH_OPEN_MARK",
    "DROP TRIGGER AGORA_AUTH_OPEN_GUARD",
)


def _backfill_viewer_publication_versions(apps: Any, schema_editor: Any) -> None:
    authorization = apps.get_model("persistence", "RenderAuthorization")
    authorization.objects.filter(audience="viewer").update(
        publication_version=1,
        authorized_open_captured_at=F("created_at"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0013_ownership_transfer_invariants"),
    ]

    operations = [
        migrations.AddField(
            model_name="renderauthorization",
            name="publication_version",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="renderauthorization",
            name="authorized_open_captured_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            _backfill_viewer_publication_versions,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="renderauthorization",
            constraint=models.CheckConstraint(
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
        ),
        migrations.CreateModel(
            name="AuthorizedOpen",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("publication_version", models.PositiveBigIntegerField()),
                ("opened_at", models.DateTimeField()),
                ("aggregated_at", models.DateTimeField(blank=True, null=True)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="authorized_open_events",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "revision",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="authorized_open_events",
                        to="persistence.revision",
                    ),
                ),
                (
                    "source_authorization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="authorized_open",
                        to="persistence.renderauthorization",
                    ),
                ),
                (
                    "viewer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="authorized_open_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_AUTHORIZED_OPEN",
                "indexes": [
                    models.Index(
                        fields=["aggregated_at", "opened_at", "id"],
                        name="agora_open_pending_idx",
                    ),
                    models.Index(
                        fields=["opened_at", "id"],
                        name="agora_open_retention_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(publication_version__gt=0),
                        name="agora_open_pub_version_pos",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DashboardOpenDaily",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("day", models.DateField()),
                ("authorized_open_count", models.PositiveBigIntegerField(default=0)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="daily_open_rollups",
                        to="persistence.dashboard",
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_OPEN_DAILY",
                "indexes": [
                    models.Index(
                        fields=["dashboard", "-day", "-id"],
                        name="agora_open_daily_read_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("dashboard", "day"),
                        name="agora_open_daily_dash_day_uq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DashboardViewerOpenSummary",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("authorized_open_count", models.PositiveBigIntegerField(default=0)),
                ("first_opened_at", models.DateTimeField()),
                ("last_opened_at", models.DateTimeField()),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="viewer_open_summaries",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "viewer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dashboard_open_summaries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_VIEWER_OPEN_SUM",
                "indexes": [
                    models.Index(
                        fields=["dashboard", "-authorized_open_count", "viewer"],
                        name="agora_view_sum_rank_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("dashboard", "viewer"),
                        name="agora_view_sum_dash_viewer_uq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(last_opened_at__gte=models.F("first_opened_at")),
                        name="agora_view_sum_time_order",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="DashboardOpenSnapshot",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("authorized_open_count", models.PositiveBigIntegerField(default=0)),
                ("last_opened_at", models.DateTimeField()),
                ("captured_through_open_id", models.PositiveBigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "dashboard",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="open_snapshot",
                        to="persistence.dashboard",
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_OPEN_SNAPSHOT",
                "indexes": [
                    models.Index(
                        fields=["-authorized_open_count", "dashboard"],
                        name="agora_open_snapshot_rank_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="AnalyticsPipelineCheckpoint",
            fields=[
                (
                    "pipeline_key",
                    models.CharField(max_length=32, primary_key=True, serialize=False),
                ),
                ("last_completed_open_id", models.PositiveBigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "TB_TA_AGORA_ANALYTICS_CKPT",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(pipeline_key="authorized_opens_v1"),
                        name="agora_analytics_ckpt_key",
                    )
                ],
            },
        ),
        migrations.RunSQL(
            sql=(
                AUTHORIZED_OPEN_GUARD_SQL,
                RENDER_CAPTURE_MARKER_GUARD_SQL,
                AUTHORIZED_OPEN_MARK_TRIGGER_SQL,
                ANALYTICS_CHECKPOINT_GUARD_SQL,
            ),
            reverse_sql=DROP_ANALYTICS_GUARDS_SQL,
        ),
    ]
