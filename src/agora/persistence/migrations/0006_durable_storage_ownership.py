from django.db import migrations, models

FORWARD_GUARD_SQL = """
CREATE OR REPLACE FUNCTION agora_guard_reservation_mutation() RETURNS trigger
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
    IF NEW.storage_state IS DISTINCT FROM OLD.storage_state
       AND NOT (
           OLD.storage_state = 'reserved'
           AND NEW.storage_state IN ('owned', 'collision')
       ) THEN
        RAISE EXCEPTION 'storage reservation state transition is not allowed'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.cleanup_required = TRUE AND NEW.cleanup_required = FALSE THEN
        RAISE EXCEPTION 'storage cleanup requirement cannot be cleared'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$
"""

REVERSE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION agora_guard_reservation_mutation() RETURNS trigger
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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0005_harden_domain_invariants"),
    ]

    operations = [
        migrations.AddField(
            model_name="storagereservation",
            name="storage_state",
            field=models.CharField(
                choices=[
                    ("reserved", "Reserved"),
                    ("owned", "Owned bytes"),
                    ("collision", "Collision; preserve bytes"),
                ],
                default="reserved",
                editable=False,
                max_length=10,
            ),
        ),
        migrations.RunSQL(
            sql=(
                "UPDATE persistence_storagereservation SET storage_state = 'owned' "
                "WHERE verified_size IS NOT NULL"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name="storagereservation",
            constraint=models.CheckConstraint(
                condition=models.Q(storage_state__in=["reserved", "owned", "collision"]),
                name="agora_reservation_valid_storage_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="storagereservation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        storage_state__in=["reserved", "collision"],
                        verified_sha256="",
                        verified_size__isnull=True,
                    )
                    | (
                        models.Q(storage_state="owned", verified_size__isnull=False)
                        & ~models.Q(verified_sha256="")
                    )
                ),
                name="agora_reservation_state_receipt_match",
            ),
        ),
        migrations.RunSQL(sql=FORWARD_GUARD_SQL, reverse_sql=REVERSE_GUARD_SQL),
    ]
