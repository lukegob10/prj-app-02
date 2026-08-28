"""Permit explicit ownership transfer without rewriting historical attribution."""

import uuid
from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DROP_HISTORICAL_OWNER_FKS_SQL = (
    "ALTER TABLE TB_TA_AGORA_REVISION DROP CONSTRAINT AGORA_REV_CREATOR_OWNER_FK",
    "ALTER TABLE TB_TA_AGORA_VIEWER_GRANT DROP CONSTRAINT AGORA_GRANT_CREATOR_OWNER_FK",
    "ALTER TABLE TB_TA_AGORA_VIEWER_GRANT DROP CONSTRAINT AGORA_GRANT_REVOKER_OWNER_FK",
)

RESTORE_HISTORICAL_OWNER_FKS_SQL = (
    """
    ALTER TABLE TB_TA_AGORA_REVISION
    ADD CONSTRAINT AGORA_REV_CREATOR_OWNER_FK
    FOREIGN KEY (dashboard_id, created_by_id)
    REFERENCES TB_TA_AGORA_DASHBOARD (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE TB_TA_AGORA_VIEWER_GRANT
    ADD CONSTRAINT AGORA_GRANT_CREATOR_OWNER_FK
    FOREIGN KEY (dashboard_id, created_by_id)
    REFERENCES TB_TA_AGORA_DASHBOARD (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
    """
    ALTER TABLE TB_TA_AGORA_VIEWER_GRANT
    ADD CONSTRAINT AGORA_GRANT_REVOKER_OWNER_FK
    FOREIGN KEY (dashboard_id, revoked_by_id)
    REFERENCES TB_TA_AGORA_DASHBOARD (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED
    """,
)


TRANSFER_CHAIN_INDEX_SQL = """
CREATE UNIQUE INDEX AGORA_TRANSFER_CHAIN_UQ_IDX
ON TB_TA_AGORA_DASH_TRANSFER (
    dashboard_id,
    NVL(previous_transfer_id, HEXTORAW('00000000000000000000000000000000'))
)
"""


DASHBOARD_TRANSFER_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_DASHBOARD_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_DASHBOARD
FOR EACH ROW
DECLARE
    description_changed BOOLEAN := FALSE;
    marker_dashboard RAW(16);
    marker_from_owner RAW(16);
    marker_to_owner RAW(16);
    marker_previous RAW(16);
    incoming_active_grants NUMBER;
    incoming_pending_requests NUMBER;
    outgoing_owner_active NUMBER(1);
    incoming_owner_active NUMBER(1);
BEGIN
    IF DELETING THEN
        raise_application_error(-20002, 'dashboards cannot be hard-deleted');
    END IF;
    IF INSERTING THEN
        IF :NEW.state <> 'draft'
           OR :NEW.latest_revision_id IS NOT NULL
           OR :NEW.published_revision_id IS NOT NULL
           OR :NEW.first_published_at IS NOT NULL
           OR :NEW.last_ownership_transfer_id IS NOT NULL THEN
            raise_application_error(-20002, 'new dashboards must begin as private drafts');
        END IF;
        RETURN;
    END IF;
    IF :NEW.id <> :OLD.id THEN
        raise_application_error(-20002, 'dashboard identity is immutable');
    END IF;

    IF :NEW.owner_id <> :OLD.owner_id THEN
        IF :OLD.state NOT IN ('draft', 'published', 'unpublished')
           OR :NEW.state <> :OLD.state
           OR :NEW.last_ownership_transfer_id IS NULL
           OR :NEW.last_ownership_transfer_id = :OLD.last_ownership_transfer_id THEN
            raise_application_error(-20002, 'ownership transfer state is invalid');
        END IF;
        IF :NEW.name <> :OLD.name
           OR :NEW.latest_revision_id <> :OLD.latest_revision_id
           OR (:NEW.latest_revision_id IS NULL AND :OLD.latest_revision_id IS NOT NULL)
           OR (:NEW.latest_revision_id IS NOT NULL AND :OLD.latest_revision_id IS NULL)
           OR :NEW.published_revision_id <> :OLD.published_revision_id
           OR (:NEW.published_revision_id IS NULL AND :OLD.published_revision_id IS NOT NULL)
           OR (:NEW.published_revision_id IS NOT NULL AND :OLD.published_revision_id IS NULL)
           OR :NEW.first_published_at <> :OLD.first_published_at
           OR (:NEW.first_published_at IS NULL AND :OLD.first_published_at IS NOT NULL)
           OR (:NEW.first_published_at IS NOT NULL AND :OLD.first_published_at IS NULL)
           OR :NEW.publication_version <> :OLD.publication_version
           OR :NEW.last_published_at <> :OLD.last_published_at
           OR (:NEW.last_published_at IS NULL AND :OLD.last_published_at IS NOT NULL)
           OR (:NEW.last_published_at IS NOT NULL AND :OLD.last_published_at IS NULL)
           OR NVL(:NEW.publication_note, CHR(0)) <> NVL(:OLD.publication_note, CHR(0))
           OR :NEW.data_as_of <> :OLD.data_as_of
           OR (:NEW.data_as_of IS NULL AND :OLD.data_as_of IS NOT NULL)
           OR (:NEW.data_as_of IS NOT NULL AND :OLD.data_as_of IS NULL)
           OR :NEW.freshness_interval_seconds <> :OLD.freshness_interval_seconds
           OR (
               :NEW.freshness_interval_seconds IS NULL
               AND :OLD.freshness_interval_seconds IS NOT NULL
           )
           OR (
               :NEW.freshness_interval_seconds IS NOT NULL
               AND :OLD.freshness_interval_seconds IS NULL
           )
           OR :NEW.freshness_confirmed_at <> :OLD.freshness_confirmed_at
           OR (:NEW.freshness_confirmed_at IS NULL AND :OLD.freshness_confirmed_at IS NOT NULL)
           OR (:NEW.freshness_confirmed_at IS NOT NULL AND :OLD.freshness_confirmed_at IS NULL)
           OR :NEW.stale_after <> :OLD.stale_after
           OR (:NEW.stale_after IS NULL AND :OLD.stale_after IS NOT NULL)
           OR (:NEW.stale_after IS NOT NULL AND :OLD.stale_after IS NULL)
           OR :NEW.created_at <> :OLD.created_at THEN
            raise_application_error(-20002, 'transfer must preserve dashboard state');
        END IF;
        IF :OLD.description IS NULL AND :NEW.description IS NOT NULL THEN
            description_changed := TRUE;
        ELSIF :OLD.description IS NOT NULL AND :NEW.description IS NULL THEN
            description_changed := TRUE;
        ELSIF :OLD.description IS NOT NULL
              AND DBMS_LOB.COMPARE(:OLD.description, :NEW.description) <> 0 THEN
            description_changed := TRUE;
        END IF;
        IF description_changed THEN
            raise_application_error(-20002, 'transfer must preserve dashboard description');
        END IF;

        SELECT dashboard_id, from_owner_id, to_owner_id, previous_transfer_id
        INTO marker_dashboard, marker_from_owner, marker_to_owner, marker_previous
        FROM TB_TA_AGORA_DASH_TRANSFER
        WHERE id = :NEW.last_ownership_transfer_id;

        IF marker_dashboard <> :OLD.id
           OR marker_from_owner <> :OLD.owner_id
           OR marker_to_owner <> :NEW.owner_id
           OR (marker_previous IS NULL AND :OLD.last_ownership_transfer_id IS NOT NULL)
           OR (marker_previous IS NOT NULL AND :OLD.last_ownership_transfer_id IS NULL)
           OR marker_previous <> :OLD.last_ownership_transfer_id THEN
            raise_application_error(-20002, 'ownership transfer marker is invalid');
        END IF;

        SELECT is_active
        INTO outgoing_owner_active
        FROM TB_TA_AGORA_USER
        WHERE id = :OLD.owner_id
        FOR UPDATE;
        SELECT is_active
        INTO incoming_owner_active
        FROM TB_TA_AGORA_USER
        WHERE id = :NEW.owner_id
        FOR UPDATE;
        IF outgoing_owner_active <> 1 OR incoming_owner_active <> 1 THEN
            raise_application_error(-20002, 'ownership transfer users must be active');
        END IF;

        SELECT COUNT(*)
        INTO incoming_active_grants
        FROM TB_TA_AGORA_VIEWER_GRANT
        WHERE dashboard_id = :OLD.id
          AND viewer_id = :NEW.owner_id
          AND revoked_at IS NULL;
        IF incoming_active_grants <> 0 THEN
            raise_application_error(-20002, 'incoming owner grant must be revoked');
        END IF;
        SELECT COUNT(*)
        INTO incoming_pending_requests
        FROM TB_TA_AGORA_ACCESS_REQUEST
        WHERE dashboard_id = :OLD.id
          AND requester_id = :NEW.owner_id
          AND status = 'pending';
        IF incoming_pending_requests <> 0 THEN
            raise_application_error(-20002, 'incoming owner request must be resolved');
        END IF;
        RETURN;
    END IF;

    IF (:NEW.last_ownership_transfer_id IS NULL AND :OLD.last_ownership_transfer_id IS NOT NULL)
       OR (:NEW.last_ownership_transfer_id IS NOT NULL AND :OLD.last_ownership_transfer_id IS NULL)
       OR :NEW.last_ownership_transfer_id <> :OLD.last_ownership_transfer_id THEN
        raise_application_error(-20002, 'transfer marker changes only with owner');
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
        raise_application_error(-20002, 'publication history begins only with first publication');
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
           OR :NEW.publication_version <> :OLD.publication_version
           OR :NEW.last_published_at <> :OLD.last_published_at
           OR (:NEW.last_published_at IS NULL AND :OLD.last_published_at IS NOT NULL)
           OR (:NEW.last_published_at IS NOT NULL AND :OLD.last_published_at IS NULL)
           OR NVL(:NEW.publication_note, CHR(0)) <> NVL(:OLD.publication_note, CHR(0))
           OR :NEW.data_as_of <> :OLD.data_as_of
           OR (:NEW.data_as_of IS NULL AND :OLD.data_as_of IS NOT NULL)
           OR (:NEW.data_as_of IS NOT NULL AND :OLD.data_as_of IS NULL)
           OR :NEW.freshness_interval_seconds <> :OLD.freshness_interval_seconds
           OR (
               :NEW.freshness_interval_seconds IS NULL
               AND :OLD.freshness_interval_seconds IS NOT NULL
           )
           OR (
               :NEW.freshness_interval_seconds IS NOT NULL
               AND :OLD.freshness_interval_seconds IS NULL
           )
           OR :NEW.freshness_confirmed_at <> :OLD.freshness_confirmed_at
           OR (:NEW.freshness_confirmed_at IS NULL AND :OLD.freshness_confirmed_at IS NOT NULL)
           OR (:NEW.freshness_confirmed_at IS NOT NULL AND :OLD.freshness_confirmed_at IS NULL)
           OR :NEW.stale_after <> :OLD.stale_after
           OR (:NEW.stale_after IS NULL AND :OLD.stale_after IS NOT NULL)
           OR (:NEW.stale_after IS NOT NULL AND :OLD.stale_after IS NULL)
           OR :NEW.created_at <> :OLD.created_at
       ) THEN
        raise_application_error(-20002, 'archived dashboards are read-only');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20002, 'ownership transfer marker is missing');
END;
"""


REVISION_AUTHORIZATION_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_REVISION_AUTH_GUARD
BEFORE INSERT ON TB_TA_AGORA_REVISION
FOR EACH ROW
DECLARE
    creator_active NUMBER(1);
    dashboard_state NVARCHAR2(16);
    current_owner RAW(16);
BEGIN
    SELECT state, owner_id
    INTO dashboard_state, current_owner
    FROM TB_TA_AGORA_DASHBOARD
    WHERE id = :NEW.dashboard_id
    FOR UPDATE;
    SELECT is_active
    INTO creator_active
    FROM TB_TA_AGORA_USER
    WHERE id = :NEW.created_by_id
    FOR UPDATE;
    IF creator_active <> 1 OR :NEW.created_by_id <> current_owner THEN
        raise_application_error(-20011, 'revision creator must be the active current owner');
    END IF;
    IF dashboard_state NOT IN ('draft', 'published', 'unpublished') THEN
        raise_application_error(-20011, 'dashboard state does not accept revisions');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20011, 'revision authorization target is missing');
END;
"""


GRANT_AUTHORIZATION_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_GRANT_IMMUT_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_VIEWER_GRANT
FOR EACH ROW
DECLARE
    current_owner RAW(16);
    current_owner_active NUMBER(1);
BEGIN
    IF DELETING THEN
        raise_application_error(-20006, 'viewer grants cannot be deleted');
    END IF;
    SELECT dashboard.owner_id, owner_user.is_active
    INTO current_owner, current_owner_active
    FROM TB_TA_AGORA_DASHBOARD dashboard
    JOIN TB_TA_AGORA_USER owner_user ON owner_user.id = dashboard.owner_id
    WHERE dashboard.id = :NEW.dashboard_id
    FOR UPDATE OF dashboard.owner_id, owner_user.is_active;

    IF INSERTING THEN
        IF current_owner_active <> 1
           OR :NEW.created_by_id <> current_owner
           OR :NEW.viewer_id = current_owner THEN
            raise_application_error(-20006, 'grant creator must be the current owner');
        END IF;
        RETURN;
    END IF;

    IF :NEW.id <> :OLD.id
       OR :NEW.dashboard_id <> :OLD.dashboard_id
       OR :NEW.viewer_id <> :OLD.viewer_id
       OR :NEW.created_by_id <> :OLD.created_by_id
       OR :NEW.created_at <> :OLD.created_at THEN
        raise_application_error(-20006, 'viewer grant relationship is immutable');
    END IF;
    IF :OLD.revoked_at IS NOT NULL THEN
        IF :NEW.revoked_at IS NULL
           OR :NEW.revoked_at <> :OLD.revoked_at
           OR :NEW.revoked_by_id IS NULL
           OR :OLD.revoked_by_id IS NULL
           OR :NEW.revoked_by_id <> :OLD.revoked_by_id THEN
            raise_application_error(-20006, 'viewer grant revocation is immutable');
        END IF;
        RETURN;
    END IF;
    IF :NEW.revoked_at IS NULL THEN
        IF :NEW.revoked_by_id IS NOT NULL THEN
            raise_application_error(-20006, 'grant revocation fields do not match');
        END IF;
        RETURN;
    END IF;
    IF current_owner_active <> 1
       OR :NEW.revoked_by_id IS NULL
       OR :NEW.revoked_by_id <> current_owner THEN
        raise_application_error(-20006, 'grant revoker must be the current owner');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20006, 'grant dashboard is missing');
END;
"""


TRANSFER_HISTORY_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_TRANSFER_HISTORY_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_DASH_TRANSFER
FOR EACH ROW
DECLARE
    current_owner RAW(16);
    current_marker RAW(16);
    dashboard_state NVARCHAR2(16);
    incoming_active NUMBER(1);
    outgoing_active NUMBER(1);
BEGIN
    IF UPDATING OR DELETING THEN
        raise_application_error(-20024, 'ownership transfer history is append-only');
    END IF;
    SELECT dashboard.owner_id,
           dashboard.last_ownership_transfer_id,
           dashboard.state,
           owner_user.is_active
    INTO current_owner, current_marker, dashboard_state, outgoing_active
    FROM TB_TA_AGORA_DASHBOARD dashboard
    JOIN TB_TA_AGORA_USER owner_user ON owner_user.id = dashboard.owner_id
    WHERE dashboard.id = :NEW.dashboard_id
    FOR UPDATE OF dashboard.owner_id, owner_user.is_active;
    SELECT is_active
    INTO incoming_active
    FROM TB_TA_AGORA_USER
    WHERE id = :NEW.to_owner_id
    FOR UPDATE;
    IF dashboard_state NOT IN ('draft', 'published', 'unpublished')
       OR :NEW.from_owner_id <> current_owner
       OR :NEW.to_owner_id = current_owner
       OR outgoing_active <> 1
       OR incoming_active <> 1
       OR (:NEW.previous_transfer_id IS NULL AND current_marker IS NOT NULL)
       OR (:NEW.previous_transfer_id IS NOT NULL AND current_marker IS NULL)
       OR :NEW.previous_transfer_id <> current_marker THEN
        raise_application_error(-20024, 'ownership transfer marker is invalid');
    END IF;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        raise_application_error(-20024, 'ownership transfer target is missing');
END;
"""


LEGACY_DASHBOARD_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_DASHBOARD_GUARD
BEFORE INSERT OR UPDATE OR DELETE ON TB_TA_AGORA_DASHBOARD
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
        raise_application_error(-20002, 'publication history begins only with first publication');
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
           OR :NEW.publication_version <> :OLD.publication_version
           OR :NEW.last_published_at <> :OLD.last_published_at
           OR (:NEW.last_published_at IS NULL AND :OLD.last_published_at IS NOT NULL)
           OR (:NEW.last_published_at IS NOT NULL AND :OLD.last_published_at IS NULL)
           OR NVL(:NEW.publication_note, CHR(0)) <> NVL(:OLD.publication_note, CHR(0))
           OR :NEW.data_as_of <> :OLD.data_as_of
           OR (:NEW.data_as_of IS NULL AND :OLD.data_as_of IS NOT NULL)
           OR (:NEW.data_as_of IS NOT NULL AND :OLD.data_as_of IS NULL)
           OR :NEW.freshness_interval_seconds <> :OLD.freshness_interval_seconds
           OR (
               :NEW.freshness_interval_seconds IS NULL
               AND :OLD.freshness_interval_seconds IS NOT NULL
           )
           OR (
               :NEW.freshness_interval_seconds IS NOT NULL
               AND :OLD.freshness_interval_seconds IS NULL
           )
           OR :NEW.freshness_confirmed_at <> :OLD.freshness_confirmed_at
           OR (:NEW.freshness_confirmed_at IS NULL AND :OLD.freshness_confirmed_at IS NOT NULL)
           OR (:NEW.freshness_confirmed_at IS NOT NULL AND :OLD.freshness_confirmed_at IS NULL)
           OR :NEW.stale_after <> :OLD.stale_after
           OR (:NEW.stale_after IS NULL AND :OLD.stale_after IS NOT NULL)
           OR (:NEW.stale_after IS NOT NULL AND :OLD.stale_after IS NULL)
           OR :NEW.created_at <> :OLD.created_at
       ) THEN
        raise_application_error(-20002, 'archived dashboards are read-only');
    END IF;
END;
"""


LEGACY_REVISION_AUTHORIZATION_GUARD_SQL = """
CREATE OR REPLACE TRIGGER AGORA_REVISION_AUTH_GUARD
BEFORE INSERT ON TB_TA_AGORA_REVISION
FOR EACH ROW
DECLARE
    creator_active NUMBER(1);
    dashboard_state NVARCHAR2(16);
BEGIN
    SELECT is_active INTO creator_active
    FROM TB_TA_AGORA_USER WHERE id = :NEW.created_by_id;
    SELECT state INTO dashboard_state
    FROM TB_TA_AGORA_DASHBOARD WHERE id = :NEW.dashboard_id;
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
"""


LEGACY_GRANT_GUARD_SQL = """
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


def _reject_reversal_after_transfer(apps: Any, schema_editor: Any) -> None:
    transfer = apps.get_model("persistence", "DashboardOwnershipTransfer")
    if transfer.objects.exists():
        raise RuntimeError(
            "0013 cannot be reversed after an ownership transfer; roll forward or restore "
            "a pre-transfer backup, and never rewrite retained actor history"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0012_enhancement_core_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="DashboardOwnershipTransfer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("transferred_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dashboard",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ownership_transfers",
                        to="persistence.dashboard",
                    ),
                ),
                (
                    "from_owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outgoing_dashboard_transfers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "previous_transfer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="next_transfer",
                        to="persistence.dashboardownershiptransfer",
                    ),
                ),
                (
                    "to_owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="incoming_dashboard_transfers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "TB_TA_AGORA_DASH_TRANSFER",
                "indexes": [
                    models.Index(
                        fields=["dashboard", "-transferred_at", "id"],
                        name="agora_transfer_history_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(from_owner=models.F("to_owner")),
                        name="agora_transfer_owners_differ",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="dashboard",
            name="last_ownership_transfer",
            field=models.OneToOneField(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="current_for_dashboard",
                to="persistence.dashboardownershiptransfer",
            ),
        ),
        migrations.AddField(
            model_name="renderauthorization",
            name="owner_transfer_epoch",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="render_authorizations",
                to="persistence.dashboardownershiptransfer",
            ),
        ),
        migrations.AddConstraint(
            model_name="renderauthorization",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(owner_transfer_epoch__isnull=True)
                    | models.Q(viewer_grant__isnull=True)
                ),
                name="agora_render_epoch_grant_xor",
            ),
        ),
        migrations.RunSQL(
            sql=(
                *DROP_HISTORICAL_OWNER_FKS_SQL,
                TRANSFER_CHAIN_INDEX_SQL,
                DASHBOARD_TRANSFER_GUARD_SQL,
                REVISION_AUTHORIZATION_GUARD_SQL,
                GRANT_AUTHORIZATION_GUARD_SQL,
                TRANSFER_HISTORY_GUARD_SQL,
            ),
            reverse_sql=(
                "DROP TRIGGER AGORA_TRANSFER_HISTORY_GUARD",
                LEGACY_DASHBOARD_GUARD_SQL,
                LEGACY_REVISION_AUTHORIZATION_GUARD_SQL,
                LEGACY_GRANT_GUARD_SQL,
                "DROP INDEX AGORA_TRANSFER_CHAIN_UQ_IDX",
                *RESTORE_HISTORICAL_OWNER_FKS_SQL,
            ),
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            reverse_code=_reject_reversal_after_transfer,
        ),
    ]
