"""Add bounded discovery, access-request, publication, and freshness foundations."""

from importlib import import_module
from typing import Any, cast

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F
from django.utils import timezone

_PRE_ENHANCEMENT_MIGRATION = cast(
    Any,
    import_module("agora.core.migrations.0005_harden_domain_invariants"),
)
PRE_ENHANCEMENT_DASHBOARD_GUARD_SQL: str = str(_PRE_ENHANCEMENT_MIGRATION.FORWARD_SQL[2]).replace(
    "persistence_dashboard", "TB_TA_AGORA_DASHBOARD"
)

DASHBOARD_METADATA_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_DASH_META_GUARD
BEFORE INSERT OR UPDATE ON TB_TA_AGORA_DASHBOARD
FOR EACH ROW
BEGIN
    IF :NEW.freshness_interval_seconds IS NULL THEN
        IF :NEW.freshness_confirmed_at IS NOT NULL OR :NEW.stale_after IS NOT NULL THEN
            raise_application_error(-20021, 'freshness fields must be set together');
        END IF;
    ELSE
        IF :NEW.freshness_interval_seconds < 1
           OR :NEW.freshness_interval_seconds > 31536000
           OR :NEW.freshness_confirmed_at IS NULL
           OR :NEW.stale_after IS NULL THEN
            raise_application_error(-20021, 'freshness interval is invalid');
        END IF;
        IF :NEW.stale_after <>
           :NEW.freshness_confirmed_at
           + NUMTODSINTERVAL(:NEW.freshness_interval_seconds, 'SECOND') THEN
            raise_application_error(-20021, 'stale-after does not match freshness claim');
        END IF;
    END IF;

    IF (:NEW.publication_version = 0 AND :NEW.last_published_at IS NOT NULL)
       OR (:NEW.publication_version > 0 AND :NEW.last_published_at IS NULL) THEN
        raise_application_error(-20021, 'publication version and time do not match');
    END IF;

    IF INSERTING THEN
        IF :NEW.publication_version <> 0 OR :NEW.last_published_at IS NOT NULL THEN
            raise_application_error(-20021, 'new dashboards have no publication version');
        END IF;
        RETURN;
    END IF;

    IF :NEW.publication_version < :OLD.publication_version
       OR :NEW.publication_version > :OLD.publication_version + 1 THEN
        raise_application_error(-20021, 'publication version must advance by one');
    END IF;

    IF :NEW.publication_version = :OLD.publication_version + 1 THEN
        IF :NEW.state <> 'published'
           OR :NEW.published_revision_id IS NULL
           OR :NEW.last_published_at IS NULL
           OR (
               :OLD.last_published_at IS NOT NULL
               AND :NEW.last_published_at < :OLD.last_published_at
           ) THEN
            raise_application_error(-20021, 'publication version lacks a valid release');
        END IF;
    ELSE
        IF (:NEW.last_published_at IS NULL AND :OLD.last_published_at IS NOT NULL)
           OR (:NEW.last_published_at IS NOT NULL AND :OLD.last_published_at IS NULL)
           OR :NEW.last_published_at <> :OLD.last_published_at
           OR NVL(:NEW.publication_note, CHR(0)) <>
              NVL(:OLD.publication_note, CHR(0)) THEN
            raise_application_error(-20021, 'release metadata changes require a new version');
        END IF;
        IF :NEW.state = 'published'
           AND :OLD.state = 'published'
           AND (
               (:NEW.published_revision_id IS NULL AND :OLD.published_revision_id IS NOT NULL)
               OR (
                   :NEW.published_revision_id IS NOT NULL
                   AND :OLD.published_revision_id IS NULL
               )
               OR :NEW.published_revision_id <> :OLD.published_revision_id
           ) THEN
            raise_application_error(-20021, 'republish must advance publication version');
        END IF;
    END IF;
END;
"""


VIEWER_STATE_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_VIEW_STATE_GUARD
BEFORE INSERT OR UPDATE ON TB_TA_AGORA_DASH_VIEWER_STATE
FOR EACH ROW
DECLARE
    current_publication_version NUMBER(19);
BEGIN
    SELECT publication_version
    INTO current_publication_version
    FROM TB_TA_AGORA_DASHBOARD
    WHERE id = :NEW.dashboard_id;
    IF :NEW.seen_publication_version > current_publication_version THEN
        raise_application_error(-20022, 'seen version exceeds dashboard publication');
    END IF;
    IF INSERTING THEN
        RETURN;
    END IF;
    IF :NEW.id <> :OLD.id
       OR :NEW.user_id <> :OLD.user_id
       OR :NEW.dashboard_id <> :OLD.dashboard_id THEN
        raise_application_error(-20022, 'viewer-state scope is immutable');
    END IF;
    IF :NEW.last_viewed_at < :OLD.last_viewed_at
       OR :NEW.seen_publication_version < :OLD.seen_publication_version THEN
        raise_application_error(-20022, 'viewer state cannot move backward');
    END IF;
END;
"""


ACCESS_REQUEST_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_ACCESS_REQUEST_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_ACCESS_REQUEST
FOR EACH ROW
DECLARE
    current_owner RAW(16);
    dashboard_state NVARCHAR2(16);
    owner_active NUMBER(1);
    requester_active NUMBER(1);
BEGIN
    IF DELETING THEN
        raise_application_error(-20023, 'access requests are retained');
    END IF;

    SELECT d.owner_id, d.state, u.is_active
    INTO current_owner, dashboard_state, owner_active
    FROM TB_TA_AGORA_DASHBOARD d
    JOIN TB_TA_AGORA_USER u ON u.id = d.owner_id
    WHERE d.id = :NEW.dashboard_id
    FOR UPDATE OF d.owner_id, u.is_active;

    SELECT is_active
    INTO requester_active
    FROM TB_TA_AGORA_USER
    WHERE id = :NEW.requester_id
    FOR UPDATE;

    IF INSERTING THEN
        IF :NEW.status <> 'pending'
           OR :NEW.resolved_at IS NOT NULL
           OR :NEW.resolved_by_id IS NOT NULL
           OR :NEW.requester_id = current_owner
           OR dashboard_state <> 'published'
           OR owner_active <> 1
           OR requester_active <> 1 THEN
            raise_application_error(-20023, 'new access request is invalid');
        END IF;
        RETURN;
    END IF;

    IF :NEW.id <> :OLD.id
       OR :NEW.dashboard_id <> :OLD.dashboard_id
       OR :NEW.requester_id <> :OLD.requester_id THEN
        raise_application_error(-20023, 'access-request scope is immutable');
    END IF;

    IF :OLD.status = 'pending' THEN
        IF :NEW.status = 'pending' THEN
            IF :NEW.requested_at <> :OLD.requested_at
               OR :NEW.resolved_at IS NOT NULL
               OR :NEW.resolved_by_id IS NOT NULL THEN
                raise_application_error(-20023, 'pending request time is immutable');
            END IF;
            RETURN;
        END IF;
        IF :NEW.status NOT IN ('approved', 'denied', 'cancelled')
           OR :NEW.resolved_at IS NULL
           OR :NEW.resolved_by_id IS NULL
           OR :NEW.requested_at <> :OLD.requested_at
           OR NVL(:NEW.message, CHR(0)) <> NVL(:OLD.message, CHR(0)) THEN
            raise_application_error(-20023, 'access-request resolution is invalid');
        END IF;
        IF (:NEW.status = 'cancelled' AND :NEW.resolved_by_id <> :NEW.requester_id)
           OR (
               :NEW.status IN ('approved', 'denied')
               AND :NEW.resolved_by_id <> current_owner
           ) THEN
            raise_application_error(-20023, 'access-request resolver is not authorized');
        END IF;
        IF :NEW.status = 'approved'
           AND (
               dashboard_state NOT IN ('published', 'unpublished')
               OR owner_active <> 1
               OR requester_active <> 1
           ) THEN
            raise_application_error(-20023, 'access request cannot be approved now');
        END IF;
        RETURN;
    END IF;

    IF :NEW.status = 'pending' THEN
        IF :NEW.requested_at <= :OLD.requested_at
           OR :NEW.resolved_at IS NOT NULL
           OR :NEW.resolved_by_id IS NOT NULL
           OR :NEW.requester_id = current_owner
           OR dashboard_state <> 'published'
           OR owner_active <> 1
           OR requester_active <> 1 THEN
            raise_application_error(-20023, 'access-request reopen is invalid');
        END IF;
        RETURN;
    END IF;

    IF :NEW.status <> :OLD.status
       OR NVL(:NEW.message, CHR(0)) <> NVL(:OLD.message, CHR(0))
       OR :NEW.requested_at <> :OLD.requested_at
       OR :NEW.resolved_at <> :OLD.resolved_at
       OR :NEW.resolved_by_id <> :OLD.resolved_by_id THEN
        raise_application_error(-20023, 'resolved access request is immutable until reopened');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20023, 'access-request target is missing');
END;
"""


DROP_CORE_GUARDS_SQL = (
    "DROP TRIGGER AGORA_ACCESS_REQUEST_GUARD",
    "DROP TRIGGER AGORA_VIEW_STATE_GUARD",
    "DROP TRIGGER AGORA_DASH_META_GUARD",
    PRE_ENHANCEMENT_DASHBOARD_GUARD_SQL,
)


def _backfill_publication_versions(apps: Any, schema_editor: Any) -> None:
    dashboard = apps.get_model("persistence", "Dashboard")
    dashboard.objects.filter(first_published_at__isnull=False).update(
        publication_version=1,
        last_published_at=F("first_published_at"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0011_project_viewer_epochs"),
    ]

    operations = [
        migrations.AddField(
            model_name="dashboard",
            name="publication_version",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="last_published_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="publication_note",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="data_as_of",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="freshness_interval_seconds",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="freshness_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dashboard",
            name="stale_after",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            _backfill_publication_versions,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="dashboard",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(publication_version=0, last_published_at__isnull=True)
                    | models.Q(publication_version__gt=0, last_published_at__isnull=False)
                ),
                name="agora_dash_pub_version_time",
            ),
        ),
        migrations.AddConstraint(
            model_name="dashboard",
            constraint=models.CheckConstraint(
                condition=(~models.Q(state="published") | models.Q(publication_version__gt=0)),
                name="agora_dash_published_versioned",
            ),
        ),
        migrations.AddConstraint(
            model_name="dashboard",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        freshness_interval_seconds__isnull=True,
                        freshness_confirmed_at__isnull=True,
                        stale_after__isnull=True,
                    )
                    | models.Q(
                        freshness_interval_seconds__isnull=False,
                        freshness_interval_seconds__gt=0,
                        freshness_interval_seconds__lte=31_536_000,
                        freshness_confirmed_at__isnull=False,
                        stale_after__isnull=False,
                    )
                ),
                name="agora_dash_fresh_fields_match",
            ),
        ),
        migrations.AddIndex(
            model_name="dashboard",
            index=models.Index(
                fields=["owner", "stale_after", "id"],
                name="agora_dash_owner_stale_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="dashboard",
            index=models.Index(fields=["stale_after", "id"], name="agora_dash_stale_scan_idx"),
        ),
        migrations.CreateModel(
            name="DashboardTag",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("label", models.CharField(max_length=40)),
                ("key", models.CharField(max_length=80)),
                ("slot", models.PositiveSmallIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tags",
                        to="persistence.dashboard",
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_DASHBOARD_TAG",
                "ordering": ("slot", "id"),
                "indexes": [models.Index(fields=["key", "dashboard"], name="agora_tag_lookup_idx")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(slot__gte=1, slot__lte=5),
                        name="agora_tag_slot_range",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(label="") & ~models.Q(key=""),
                        name="agora_tag_values_not_empty",
                    ),
                    models.UniqueConstraint(
                        fields=("dashboard", "key"),
                        name="agora_tag_dashboard_key_uq",
                    ),
                    models.UniqueConstraint(
                        fields=("dashboard", "slot"),
                        name="agora_tag_dashboard_slot_uq",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="DashboardFavorite",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="favorites",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dashboard_favorites",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_DASH_FAVORITE",
                "indexes": [
                    models.Index(
                        fields=["user", "-created_at", "-id"],
                        name="agora_favorite_recent_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "dashboard"),
                        name="agora_favorite_user_dash_uq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="DashboardViewerState",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("last_viewed_at", models.DateTimeField()),
                ("seen_publication_version", models.PositiveBigIntegerField(default=0)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="viewer_states",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dashboard_view_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_DASH_VIEWER_STATE",
                "indexes": [
                    models.Index(
                        fields=["user", "-last_viewed_at", "-id"],
                        name="agora_view_state_recent_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "dashboard"),
                        name="agora_view_state_user_dash_uq",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="AccessRequest",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("denied", "Denied"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("message", models.CharField(blank=True, max_length=500)),
                ("requested_at", models.DateTimeField(default=timezone.now)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_requests",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "requester",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dashboard_access_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="resolved_dashboard_access_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_ACCESS_REQUEST",
                "indexes": [
                    models.Index(
                        fields=["dashboard", "status", "-requested_at", "-id"],
                        name="agora_access_owner_queue_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            status__in=["pending", "approved", "denied", "cancelled"]
                        ),
                        name="agora_access_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                status="pending",
                                resolved_at__isnull=True,
                                resolved_by__isnull=True,
                            )
                            | (
                                ~models.Q(status="pending")
                                & models.Q(
                                    resolved_at__isnull=False,
                                    resolved_by__isnull=False,
                                )
                            )
                        ),
                        name="agora_access_resolution_match",
                    ),
                    models.UniqueConstraint(
                        fields=("dashboard", "requester"),
                        name="agora_access_dash_requester_uq",
                    ),
                ],
            },
        ),
        migrations.RemoveIndex(
            model_name="viewergrant",
            name="agora_grant_viewer_active_idx",
        ),
        migrations.AddIndex(
            model_name="viewergrant",
            index=models.Index(
                fields=["viewer", "revoked_at", "-created_at", "-id"],
                name="agora_grant_viewer_active_idx",
            ),
        ),
        migrations.RunSQL(
            sql=(
                DASHBOARD_METADATA_TRIGGER_SQL,
                VIEWER_STATE_TRIGGER_SQL,
                ACCESS_REQUEST_TRIGGER_SQL,
            ),
            reverse_sql=DROP_CORE_GUARDS_SQL,
        ),
    ]
