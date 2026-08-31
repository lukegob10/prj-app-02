"""Install Oracle-native relational and retained-history invariants."""

from django.db import migrations, models

FORWARD_SQL = (
    """
    ALTER TABLE persistence_dashboard
    ADD CONSTRAINT agora_dash_latest_owned_fk
    FOREIGN KEY (id, latest_revision_id)
    REFERENCES persistence_revision (dashboard_id, id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE persistence_dashboard
    ADD CONSTRAINT agora_dash_published_owned_fk
    FOREIGN KEY (id, published_revision_id)
    REFERENCES persistence_revision (dashboard_id, id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE persistence_revision
    ADD CONSTRAINT agora_rev_creator_owner_fk
    FOREIGN KEY (dashboard_id, created_by_id)
    REFERENCES persistence_dashboard (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE persistence_viewergrant
    ADD CONSTRAINT agora_grant_creator_owner_fk
    FOREIGN KEY (dashboard_id, created_by_id)
    REFERENCES persistence_dashboard (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE persistence_viewergrant
    ADD CONSTRAINT agora_grant_revoker_owner_fk
    FOREIGN KEY (dashboard_id, revoked_by_id)
    REFERENCES persistence_dashboard (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    CREATE UNIQUE INDEX agora_artifact_one_html_idx
    ON persistence_artifact (
        CASE WHEN kind = 'html' THEN revision_id ELSE NULL END
    )
    """,
    """
    CREATE OR REPLACE TRIGGER agora_user_retention_guard
    BEFORE UPDATE OR DELETE ON persistence_user
    FOR EACH ROW
    BEGIN
        IF DELETING THEN
            raise_application_error(-20001, 'users cannot be hard-deleted');
        END IF;
        IF :NEW.id <> :OLD.id OR :NEW.soeid <> :OLD.soeid THEN
            raise_application_error(-20001, 'user identity is immutable');
        END IF;
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
    CREATE OR REPLACE TRIGGER agora_revision_immut_guard
    BEFORE UPDATE OR DELETE ON persistence_revision
    FOR EACH ROW
    BEGIN
        IF DELETING THEN
            raise_application_error(-20003, 'complete revisions cannot be deleted');
        END IF;
        IF :OLD.artifacts_locked = 0
           AND :NEW.artifacts_locked = 1
           AND :NEW.id = :OLD.id
           AND :NEW.dashboard_id = :OLD.dashboard_id
           AND :NEW.number = :OLD.number
           AND :NEW.created_by_id = :OLD.created_by_id
           AND :NEW.created_at = :OLD.created_at THEN
            RETURN;
        END IF;
        raise_application_error(-20003, 'complete revisions are immutable');
    END;
    """,
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
    CREATE OR REPLACE TRIGGER agora_artifact_immut_guard
    BEFORE UPDATE OR DELETE ON persistence_artifact
    FOR EACH ROW
    BEGIN
        raise_application_error(-20005, 'artifact rows are append-only');
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
    """
    CREATE OR REPLACE TRIGGER agora_audit_append_guard
    BEFORE INSERT OR UPDATE OR DELETE ON persistence_auditevent
    FOR EACH ROW
    DECLARE
        document JSON_ELEMENT_T;
    BEGIN
        IF UPDATING OR DELETING THEN
            raise_application_error(-20007, 'audit events are append-only');
        END IF;
        document := JSON_ELEMENT_T.parse(:NEW.metadata);
        IF NOT document.is_object THEN
            raise_application_error(-20007, 'audit metadata must be a JSON object');
        END IF;
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_revision_complete_guard
    AFTER UPDATE OF artifacts_locked ON persistence_revision
    FOR EACH ROW
    DECLARE
        html_count NUMBER;
    BEGIN
        IF :OLD.artifacts_locked = 0 AND :NEW.artifacts_locked = 1 THEN
            SELECT COUNT(*)
            INTO html_count
            FROM persistence_artifact
            WHERE revision_id = :NEW.id AND kind = 'html';
            IF html_count <> 1 THEN
                raise_application_error(
                    -20008,
                    'a committed revision requires exactly one HTML artifact'
                );
            END IF;
        END IF;
    END;
    """,
    """
    CREATE OR REPLACE TRIGGER agora_dashboard_latest_guard
    AFTER UPDATE OF latest_revision_id ON persistence_dashboard
    FOR EACH ROW
    DECLARE
        expected_latest VARCHAR2(32);
    BEGIN
        SELECT MAX(id) KEEP (DENSE_RANK LAST ORDER BY "NUMBER")
        INTO expected_latest
        FROM persistence_revision
        WHERE dashboard_id = :NEW.id;
        IF :NEW.latest_revision_id <> expected_latest
           OR (:NEW.latest_revision_id IS NULL AND expected_latest IS NOT NULL)
           OR (:NEW.latest_revision_id IS NOT NULL AND expected_latest IS NULL) THEN
            raise_application_error(-20009, 'dashboard latest revision pointer is inconsistent');
        END IF;
    END;
    """,
)

REVERSE_SQL = (
    "DROP TRIGGER agora_dashboard_latest_guard",
    "DROP TRIGGER agora_revision_complete_guard",
    "DROP TRIGGER agora_audit_append_guard",
    "DROP TRIGGER agora_grant_immut_guard",
    "DROP TRIGGER agora_artifact_immut_guard",
    "DROP TRIGGER agora_artifact_insert_guard",
    "DROP TRIGGER agora_revision_immut_guard",
    "DROP TRIGGER agora_dashboard_guard",
    "DROP TRIGGER agora_user_retention_guard",
    "DROP INDEX agora_artifact_one_html_idx",
    "ALTER TABLE persistence_viewergrant DROP CONSTRAINT agora_grant_revoker_owner_fk",
    "ALTER TABLE persistence_viewergrant DROP CONSTRAINT agora_grant_creator_owner_fk",
    "ALTER TABLE persistence_revision DROP CONSTRAINT agora_rev_creator_owner_fk",
    "ALTER TABLE persistence_dashboard DROP CONSTRAINT agora_dash_published_owned_fk",
    "ALTER TABLE persistence_dashboard DROP CONSTRAINT agora_dash_latest_owned_fk",
)


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0001_initial_domain"),
    ]

    operations = [
        migrations.AddField(
            model_name="revision",
            name="artifacts_locked",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddConstraint(
            model_name="viewergrant",
            constraint=models.CheckConstraint(
                condition=models.Q(("viewer", models.F("created_by")), _negated=True),
                name="agora_grant_owner_not_viewer",
            ),
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
