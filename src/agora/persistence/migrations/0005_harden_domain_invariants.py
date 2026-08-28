"""Harden Oracle lifecycle, authorization, and canonical-name boundaries."""

from django.db import migrations, models

FORWARD_SQL = (
    """
    CREATE OR REPLACE TRIGGER agora_artifact_insert_guard
    BEFORE INSERT ON persistence_artifact
    FOR EACH ROW
    DECLARE
        parent_locked NUMBER(1);
    BEGIN
        IF :NEW.logical_name <> COMPOSE(:NEW.logical_name)
           OR :NEW.name_key <> COMPOSE(:NEW.name_key)
           OR :NEW.name_key <> NLS_LOWER(:NEW.name_key) THEN
            raise_application_error(-20004, 'artifact names must be canonical');
        END IF;
        SELECT artifacts_locked
        INTO parent_locked
        FROM persistence_revision
        WHERE id = :NEW.revision_id;
        IF parent_locked <> 0 THEN
            raise_application_error(-20004, 'artifacts cannot be added to a complete revision');
        END IF;
    EXCEPTION
        WHEN NO_DATA_FOUND THEN
            raise_application_error(-20004, 'artifact revision does not exist');
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_reservation_mut_guard
    BEFORE UPDATE ON persistence_storagereservation
    FOR EACH ROW
    BEGIN
        IF :NEW.id <> :OLD.id
           OR :NEW.storage_key <> :OLD.storage_key
           OR :NEW.created_at <> :OLD.created_at
           OR :NEW.expires_at <> :OLD.expires_at THEN
            raise_application_error(-20010, 'storage reservation identity is immutable');
        END IF;
        IF :OLD.verified_size IS NOT NULL
           AND (
               :NEW.verified_size IS NULL
               OR :NEW.verified_size <> :OLD.verified_size
               OR :NEW.verified_sha256 IS NULL
               OR :NEW.verified_sha256 <> :OLD.verified_sha256
           ) THEN
            raise_application_error(-20010, 'storage verification receipt is immutable');
        END IF;
        IF :OLD.cleanup_required = 1 AND :NEW.cleanup_required = 0 THEN
            raise_application_error(-20010, 'storage cleanup requirement cannot be cleared');
        END IF;
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_dashboard_guard
    BEFORE INSERT OR UPDATE OR DELETE ON persistence_dashboard
    FOR EACH ROW
    DECLARE
        description_changed BOOLEAN := FALSE;
    BEGIN
        IF DELETING THEN
            raise_application_error(-20002, 'dashboards cannot be hard-deleted');
        END IF;
        IF INSERTING THEN
            IF :NEW.state <> 'draft'
               OR :NEW.latest_revision_id IS NOT NULL
               OR :NEW.published_revision_id IS NOT NULL
               OR :NEW.first_published_at IS NOT NULL THEN
                raise_application_error(-20002, 'new dashboards must begin as private drafts');
            END IF;
            RETURN;
        END IF;
        IF :NEW.id <> :OLD.id OR :NEW.owner_id <> :OLD.owner_id THEN
            raise_application_error(-20002, 'dashboard identity and ownership are immutable');
        END IF;
        IF :OLD.state = 'deleted' THEN
            raise_application_error(-20002, 'deleted dashboards are terminal tombstones');
        END IF;
        IF :OLD.first_published_at IS NOT NULL
           AND (
               :NEW.first_published_at IS NULL
               OR :NEW.first_published_at <> :OLD.first_published_at
           ) THEN
            raise_application_error(-20002, 'first publication time is immutable');
        END IF;
        IF :OLD.first_published_at IS NULL
           AND :NEW.first_published_at IS NOT NULL
           AND NOT (:OLD.state = 'draft' AND :NEW.state = 'published') THEN
            raise_application_error(
                -20002,
                'publication history begins only with first publication'
            );
        END IF;
        IF NOT (
            (:OLD.state = 'draft' AND :NEW.state IN ('draft', 'published', 'archived', 'deleted'))
            OR (
                :OLD.state = 'published'
                AND :NEW.state IN ('published', 'unpublished', 'archived', 'deleted')
            )
            OR (
                :OLD.state = 'unpublished'
                AND :NEW.state IN ('unpublished', 'published', 'archived', 'deleted')
            )
            OR (:OLD.state = 'archived' AND :NEW.state = 'deleted')
            OR (
                :OLD.state = 'archived'
                AND :OLD.first_published_at IS NULL
                AND :NEW.state = 'draft'
            )
            OR (
                :OLD.state = 'archived'
                AND :OLD.first_published_at IS NOT NULL
                AND :NEW.state = 'unpublished'
            )
        ) THEN
            raise_application_error(-20002, 'dashboard lifecycle transition is not allowed');
        END IF;
        IF :OLD.description IS NULL AND :NEW.description IS NOT NULL THEN
            description_changed := TRUE;
        ELSIF :OLD.description IS NOT NULL AND :NEW.description IS NULL THEN
            description_changed := TRUE;
        ELSIF :OLD.description IS NOT NULL
              AND DBMS_LOB.COMPARE(:OLD.description, :NEW.description) <> 0 THEN
            description_changed := TRUE;
        END IF;
        IF :OLD.state = 'archived'
           AND (
               :NEW.name <> :OLD.name
               OR description_changed
               OR :NEW.latest_revision_id <> :OLD.latest_revision_id
               OR (:NEW.latest_revision_id IS NULL AND :OLD.latest_revision_id IS NOT NULL)
               OR (:NEW.latest_revision_id IS NOT NULL AND :OLD.latest_revision_id IS NULL)
               OR :NEW.published_revision_id <> :OLD.published_revision_id
               OR (:NEW.published_revision_id IS NULL AND :OLD.published_revision_id IS NOT NULL)
               OR (:NEW.published_revision_id IS NOT NULL AND :OLD.published_revision_id IS NULL)
               OR :NEW.first_published_at <> :OLD.first_published_at
               OR (:NEW.first_published_at IS NULL AND :OLD.first_published_at IS NOT NULL)
               OR (:NEW.first_published_at IS NOT NULL AND :OLD.first_published_at IS NULL)
               OR :NEW.created_at <> :OLD.created_at
           ) THEN
            raise_application_error(-20002, 'archived dashboards are read-only');
        END IF;
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_revision_auth_guard
    BEFORE INSERT ON persistence_revision
    FOR EACH ROW
    DECLARE
        creator_active NUMBER(1);
        dashboard_state NVARCHAR2(16);
    BEGIN
        SELECT is_active
        INTO creator_active
        FROM persistence_user
        WHERE id = :NEW.created_by_id;
        SELECT state
        INTO dashboard_state
        FROM persistence_dashboard
        WHERE id = :NEW.dashboard_id;
        IF creator_active <> 1 THEN
            raise_application_error(-20011, 'revision creator must be active');
        END IF;
        IF dashboard_state NOT IN ('draft', 'published', 'unpublished') THEN
            raise_application_error(-20011, 'dashboard state does not accept revisions');
        END IF;
    EXCEPTION
        WHEN NO_DATA_FOUND THEN
            raise_application_error(-20011, 'revision authorization target is missing');
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_grant_immut_guard
    BEFORE UPDATE OR DELETE ON persistence_viewergrant
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
    """,
)

REVERSE_SQL = (
    "DROP TRIGGER agora_revision_auth_guard",
    "DROP TRIGGER agora_reservation_mut_guard",
    """
    CREATE OR REPLACE TRIGGER agora_artifact_insert_guard
    BEFORE INSERT ON persistence_artifact
    FOR EACH ROW
    DECLARE
        parent_locked NUMBER(1);
    BEGIN
        SELECT artifacts_locked
        INTO parent_locked
        FROM persistence_revision
        WHERE id = :NEW.revision_id;
        IF parent_locked <> 0 THEN
            raise_application_error(-20004, 'artifacts cannot be added to a complete revision');
        END IF;
    EXCEPTION
        WHEN NO_DATA_FOUND THEN
            raise_application_error(-20004, 'artifact revision does not exist');
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_dashboard_guard
    BEFORE UPDATE OR DELETE ON persistence_dashboard
    FOR EACH ROW
    BEGIN
        IF DELETING THEN
            raise_application_error(-20002, 'dashboards cannot be hard-deleted');
        END IF;
        IF :NEW.id <> :OLD.id OR :NEW.owner_id <> :OLD.owner_id THEN
            raise_application_error(-20002, 'dashboard identity and ownership are immutable');
        END IF;
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_grant_immut_guard
    BEFORE UPDATE OR DELETE ON persistence_viewergrant
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
               OR :NEW.revoked_by_id <> :OLD.revoked_by_id
           ) THEN
            raise_application_error(-20006, 'viewer grant revocation is immutable');
        END IF;
    END;
    """,
)


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0004_storage_shard_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="dashboard",
            name="first_published_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="dashboard",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(first_published_at__isnull=True, state="draft")
                    | models.Q(
                        first_published_at__isnull=False,
                        state__in=["published", "unpublished"],
                    )
                    | models.Q(state__in=["archived", "deleted"])
                ),
                name="agora_dashboard_publication_history",
            ),
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
