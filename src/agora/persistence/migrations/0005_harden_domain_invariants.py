from django.db import migrations, models

FORWARD_SQL = (
    """
    ALTER TABLE persistence_artifact
    ADD CONSTRAINT agora_artifact_logical_name_nfc
    CHECK (logical_name = normalize(logical_name, NFC))
    """,
    """
    ALTER TABLE persistence_artifact
    ADD CONSTRAINT agora_artifact_name_key_canonical
    CHECK (
        name_key = normalize(
            casefold(normalize(logical_name, NFKC) COLLATE "und-x-icu"),
            NFKC
        )
    )
    """,
    """
    CREATE FUNCTION agora_guard_reservation_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF ROW(OLD.id, OLD.storage_key, OLD.created_at, OLD.expires_at)
               IS DISTINCT FROM
           ROW(NEW.id, NEW.storage_key, NEW.created_at, NEW.expires_at) THEN
            RAISE EXCEPTION 'storage reservation identity is immutable'
                USING ERRCODE = '55000';
        END IF;
        IF OLD.verified_size IS NOT NULL
           AND ROW(OLD.verified_size, OLD.verified_sha256)
               IS DISTINCT FROM ROW(NEW.verified_size, NEW.verified_sha256) THEN
            RAISE EXCEPTION 'storage verification receipt is immutable'
                USING ERRCODE = '55000';
        END IF;
        IF OLD.cleanup_required = TRUE AND NEW.cleanup_required = FALSE THEN
            RAISE EXCEPTION 'storage cleanup requirement cannot be cleared'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER agora_reservation_mutation_guard
    BEFORE UPDATE ON persistence_storagereservation
    FOR EACH ROW EXECUTE FUNCTION agora_guard_reservation_mutation()
    """,
    """
    CREATE OR REPLACE FUNCTION agora_guard_dashboard_identity() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'dashboards cannot be hard-deleted' USING ERRCODE = '55000';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
            RAISE EXCEPTION 'dashboard identity and ownership are immutable'
                USING ERRCODE = '55000';
        END IF;
        IF OLD.state = 'deleted' THEN
            RAISE EXCEPTION 'deleted dashboards are terminal tombstones'
                USING ERRCODE = '55000';
        END IF;
        IF OLD.first_published_at IS NOT NULL
           AND NEW.first_published_at IS DISTINCT FROM OLD.first_published_at THEN
            RAISE EXCEPTION 'first publication time is immutable' USING ERRCODE = '55000';
        END IF;
        IF OLD.first_published_at IS NULL
           AND NEW.first_published_at IS NOT NULL
           AND NOT (OLD.state = 'draft' AND NEW.state = 'published') THEN
            RAISE EXCEPTION 'publication history begins only with first publication'
                USING ERRCODE = '55000';
        END IF;
        IF NOT (
            (OLD.state = 'draft'
             AND NEW.state IN ('draft', 'published', 'archived', 'deleted'))
            OR (OLD.state = 'published'
                AND NEW.state IN ('published', 'unpublished', 'archived', 'deleted'))
            OR (OLD.state = 'unpublished'
                AND NEW.state IN ('unpublished', 'published', 'archived', 'deleted'))
            OR (OLD.state = 'archived' AND NEW.state = 'deleted')
            OR (OLD.state = 'archived' AND OLD.first_published_at IS NULL
                AND NEW.state = 'draft')
            OR (OLD.state = 'archived' AND OLD.first_published_at IS NOT NULL
                AND NEW.state = 'unpublished')
        ) THEN
            RAISE EXCEPTION 'dashboard lifecycle transition is not allowed'
                USING ERRCODE = '55000';
        END IF;
        IF OLD.state = 'archived'
           AND ROW(OLD.name, OLD.description, OLD.latest_revision_id,
                   OLD.published_revision_id, OLD.first_published_at, OLD.created_at)
               IS DISTINCT FROM
               ROW(NEW.name, NEW.description, NEW.latest_revision_id,
                   NEW.published_revision_id, NEW.first_published_at, NEW.created_at) THEN
            RAISE EXCEPTION 'archived dashboards are read-only' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE FUNCTION agora_guard_dashboard_creation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.state <> 'draft'
           OR NEW.latest_revision_id IS NOT NULL
           OR NEW.published_revision_id IS NOT NULL
           OR NEW.first_published_at IS NOT NULL THEN
            RAISE EXCEPTION 'new dashboards must begin as private drafts'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER agora_dashboard_creation_guard
    BEFORE INSERT ON persistence_dashboard
    FOR EACH ROW EXECUTE FUNCTION agora_guard_dashboard_creation()
    """,
    """
    CREATE FUNCTION agora_guard_revision_authorization() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        creator_active boolean;
        dashboard_state text;
    BEGIN
        SELECT is_active INTO creator_active
        FROM persistence_user
        WHERE id = NEW.created_by_id
        FOR SHARE;
        SELECT state INTO dashboard_state
        FROM persistence_dashboard
        WHERE id = NEW.dashboard_id
        FOR SHARE;
        IF creator_active IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'revision creator must be active' USING ERRCODE = '23514';
        END IF;
        IF dashboard_state IS NULL
           OR dashboard_state NOT IN ('draft', 'published', 'unpublished') THEN
            RAISE EXCEPTION 'dashboard state does not accept revisions'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER agora_revision_authorization_guard
    BEFORE INSERT ON persistence_revision
    FOR EACH ROW EXECUTE FUNCTION agora_guard_revision_authorization()
    """,
    """
    CREATE OR REPLACE FUNCTION agora_guard_grant_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'viewer grants cannot be deleted' USING ERRCODE = '55000';
        END IF;
        IF ROW(OLD.id, OLD.dashboard_id, OLD.viewer_id, OLD.created_by_id, OLD.created_at)
               IS DISTINCT FROM
               ROW(NEW.id, NEW.dashboard_id, NEW.viewer_id, NEW.created_by_id, NEW.created_at) THEN
            RAISE EXCEPTION 'viewer grant relationship is immutable' USING ERRCODE = '55000';
        END IF;
        IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT NULL
           AND ROW(OLD.revoked_at, OLD.revoked_by_id)
               IS DISTINCT FROM ROW(NEW.revoked_at, NEW.revoked_by_id) THEN
            RAISE EXCEPTION 'a recorded viewer grant revocation is immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
)


REVERSE_SQL = (
    """
    CREATE OR REPLACE FUNCTION agora_guard_grant_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'viewer grants cannot be deleted' USING ERRCODE = '55000';
        END IF;
        IF ROW(OLD.id, OLD.dashboard_id, OLD.viewer_id, OLD.created_by_id, OLD.created_at)
               IS DISTINCT FROM
               ROW(NEW.id, NEW.dashboard_id, NEW.viewer_id, NEW.created_by_id, NEW.created_at) THEN
            RAISE EXCEPTION 'viewer grant relationship is immutable' USING ERRCODE = '55000';
        END IF;
        IF OLD.revoked_at IS NOT NULL
           AND ROW(OLD.revoked_at, OLD.revoked_by_id)
               IS DISTINCT FROM ROW(NEW.revoked_at, NEW.revoked_by_id) THEN
            RAISE EXCEPTION 'viewer grant revocation is immutable' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER agora_revision_authorization_guard ON persistence_revision",
    "DROP FUNCTION agora_guard_revision_authorization()",
    "DROP TRIGGER agora_dashboard_creation_guard ON persistence_dashboard",
    "DROP FUNCTION agora_guard_dashboard_creation()",
    """
    CREATE OR REPLACE FUNCTION agora_guard_dashboard_identity() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'dashboards cannot be hard-deleted' USING ERRCODE = '55000';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
            RAISE EXCEPTION 'dashboard identity and ownership are immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER agora_reservation_mutation_guard ON persistence_storagereservation",
    "DROP FUNCTION agora_guard_reservation_mutation()",
    "ALTER TABLE persistence_artifact DROP CONSTRAINT agora_artifact_name_key_canonical",
    "ALTER TABLE persistence_artifact DROP CONSTRAINT agora_artifact_logical_name_nfc",
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
