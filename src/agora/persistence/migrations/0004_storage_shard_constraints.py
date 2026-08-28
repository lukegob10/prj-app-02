"""Keep storage-key directory shards aligned with their digest prefix."""

from django.db import migrations

FORWARD_SQL = (
    """
    ALTER TABLE persistence_artifact
    ADD CONSTRAINT agora_artifact_storage_shards
    CHECK (
        SUBSTR(storage_key, 4, 2) = SUBSTR(storage_key, 10, 2)
        AND SUBSTR(storage_key, 7, 2) = SUBSTR(storage_key, 12, 2)
    )
    """,
    """
    ALTER TABLE persistence_storagereservation
    ADD CONSTRAINT agora_reservation_storage_shards
    CHECK (
        SUBSTR(storage_key, 4, 2) = SUBSTR(storage_key, 10, 2)
        AND SUBSTR(storage_key, 7, 2) = SUBSTR(storage_key, 12, 2)
    )
    """,
)

REVERSE_SQL = (
    "ALTER TABLE persistence_storagereservation DROP CONSTRAINT agora_reservation_storage_shards",
    "ALTER TABLE persistence_artifact DROP CONSTRAINT agora_artifact_storage_shards",
)


class Migration(migrations.Migration):
    dependencies = [("persistence", "0003_allow_empty_audit_metadata")]

    operations = [migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL)]
