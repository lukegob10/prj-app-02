from django.db import migrations

FORWARD_SQL = (
    """
    ALTER TABLE persistence_artifact
    ADD CONSTRAINT agora_artifact_storage_shards
    CHECK (
        substring(storage_key FROM 4 FOR 2) = substring(storage_key FROM 10 FOR 2)
        AND substring(storage_key FROM 7 FOR 2) = substring(storage_key FROM 12 FOR 2)
    )
    """,
    """
    ALTER TABLE persistence_storagereservation
    ADD CONSTRAINT agora_reservation_storage_shards
    CHECK (
        substring(storage_key FROM 4 FOR 2) = substring(storage_key FROM 10 FOR 2)
        AND substring(storage_key FROM 7 FOR 2) = substring(storage_key FROM 12 FOR 2)
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
