"""Make viewer grants retained epochs and bind viewer render credentials to one epoch."""

import django.db.models.deletion
from django.db import migrations, models

# Oracle has no PostgreSQL-style partial indexes.  A function-based unique index
# has the same active-only semantics: revoked rows produce two NULL expression
# values and therefore do not collide, while every active row indexes its real
# dashboard/viewer identifiers.  Keep this SQL tied to the prefixed table name;
# this migration runs after 0010 has moved all Agora tables into that namespace.
ACTIVE_GRANT_INDEX_SQL = """
CREATE UNIQUE INDEX AGORA_GRANT_ACTIVE_UQ_IDX
ON TB_TA_AGORA_VIEWER_GRANT (
    CASE WHEN revoked_at IS NULL THEN dashboard_id ELSE NULL END,
    CASE WHEN revoked_at IS NULL THEN viewer_id ELSE NULL END
)
"""

DROP_ACTIVE_GRANT_INDEX_SQL = "DROP INDEX AGORA_GRANT_ACTIVE_UQ_IDX"


# 0005/0010's trigger allowed a revoked epoch to be reopened by setting both
# revocation columns back to NULL.  The forward version makes revocation
# one-way even for QuerySet.update()/bulk SQL that bypasses model.save().
IMMUTABLE_EPOCH_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_GRANT_IMMUT_GUARD
BEFORE UPDATE OR DELETE ON TB_TA_AGORA_VIEWER_GRANT
FOR EACH ROW
BEGIN
    IF DELETING THEN
        raise_application_error(-20006, 'viewer grants cannot be deleted');
    END IF;
    IF :NEW.id <> :OLD.id
       OR :NEW.dashboard_id <> :OLD.dashboard_id
       OR :NEW.viewer_id <> :OLD.viewer_id
       OR :NEW.created_by_id <> :OLD.created_by_id
       OR :NEW.created_at <> :OLD.created_at THEN
        raise_application_error(-20006, 'viewer grant relationship is immutable');
    END IF;
    IF :OLD.revoked_at IS NOT NULL
       AND (
           :NEW.revoked_at IS NULL
           OR :NEW.revoked_at <> :OLD.revoked_at
           OR :NEW.revoked_by_id IS NULL
           OR :OLD.revoked_by_id IS NULL
           OR :NEW.revoked_by_id <> :OLD.revoked_by_id
       ) THEN
        raise_application_error(-20006, 'viewer grant revocation is immutable');
    END IF;
END;
"""


# Restore the historical trigger when rolling back. Django reverses the later
# RemoveConstraint before this RunSQL, so a rollback is possible only while no
# Dashboard/viewer pair has accumulated multiple retained epochs. Once a regrant
# exists, the lifetime constraint intentionally makes rollback fail rather than
# delete audit history. Operators must then roll forward or restore a pre-0011
# backup; they must never deduplicate retained grant rows to force a reversal.
LEGACY_EPOCH_TRIGGER_SQL = """
CREATE OR REPLACE TRIGGER AGORA_GRANT_IMMUT_GUARD
BEFORE UPDATE OR DELETE ON TB_TA_AGORA_VIEWER_GRANT
FOR EACH ROW
BEGIN
    IF DELETING THEN
        raise_application_error(-20006, 'viewer grants cannot be deleted');
    END IF;
    IF :NEW.id <> :OLD.id
       OR :NEW.dashboard_id <> :OLD.dashboard_id
       OR :NEW.viewer_id <> :OLD.viewer_id
       OR :NEW.created_by_id <> :OLD.created_by_id
       OR :NEW.created_at <> :OLD.created_at THEN
        raise_application_error(-20006, 'viewer grant relationship is immutable');
    END IF;
    IF :OLD.revoked_at IS NOT NULL
       AND :NEW.revoked_at IS NOT NULL
       AND (
           :NEW.revoked_at <> :OLD.revoked_at
           OR :NEW.revoked_by_id <> :OLD.revoked_by_id
           OR (:NEW.revoked_by_id IS NULL AND :OLD.revoked_by_id IS NOT NULL)
           OR (:NEW.revoked_by_id IS NOT NULL AND :OLD.revoked_by_id IS NULL)
       ) THEN
        raise_application_error(-20006, 'viewer grant revocation is immutable');
    END IF;
END;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0010_apply_agora_project_table_prefix"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                ACTIVE_GRANT_INDEX_SQL,
                IMMUTABLE_EPOCH_TRIGGER_SQL,
            ),
            reverse_sql=(
                DROP_ACTIVE_GRANT_INDEX_SQL,
                LEGACY_EPOCH_TRIGGER_SQL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="viewergrant",
            name="agora_grant_dashboard_viewer_unique",
        ),
        migrations.AddIndex(
            model_name="viewergrant",
            index=models.Index(
                fields=["dashboard", "viewer", "revoked_at"],
                name="agora_grant_scope_active_idx",
            ),
        ),
        migrations.AddField(
            model_name="renderauthorization",
            name="viewer_grant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="render_authorizations",
                to="persistence.viewergrant",
            ),
        ),
    ]
