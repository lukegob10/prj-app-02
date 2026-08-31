"""Track durable storage ownership with an Oracle-enforced state machine."""

from django.db import migrations, models

FORWARD_GUARD_SQL = """
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
    IF :NEW.storage_state <> :OLD.storage_state
       AND NOT (
           :OLD.storage_state = 'reserved'
           AND :NEW.storage_state IN ('owned', 'collision')
       ) THEN
        raise_application_error(-20010, 'storage reservation state transition is not allowed');
    END IF;
    IF :OLD.cleanup_required = 1 AND :NEW.cleanup_required = 0 THEN
        raise_application_error(-20010, 'storage cleanup requirement cannot be cleared');
    END IF;
END;
"""

REVERSE_GUARD_SQL = """
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
        migrations.RunSQL(sql=(FORWARD_GUARD_SQL,), reverse_sql=(REVERSE_GUARD_SQL,)),
    ]
