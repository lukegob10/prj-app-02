"""Add authentication controls and Oracle-side session revocation versioning."""

from django.db import migrations, models

USER_AUTH_VERSION_SQL = """
CREATE OR REPLACE TRIGGER agora_user_auth_version_guard
BEFORE UPDATE ON persistence_user
FOR EACH ROW
BEGIN
    IF :NEW.is_active <> :OLD.is_active
       AND :NEW.auth_version <= :OLD.auth_version THEN
        :NEW.auth_version := :OLD.auth_version + 1;
    END IF;
    IF :NEW.auth_version < :OLD.auth_version THEN
        raise_application_error(-20012, 'user authentication version cannot move backwards');
    END IF;
END;
"""

USER_AUTH_VERSION_REVERSE_SQL = "DROP TRIGGER agora_user_auth_version_guard"


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0006_durable_storage_ownership"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="auth_version",
            field=models.PositiveBigIntegerField(default=1, editable=False),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(auth_version__gt=0),
                name="agora_user_auth_version_positive",
            ),
        ),
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("bucket_hash", models.CharField(editable=False, max_length=64, unique=True)),
                ("window_started_at", models.DateTimeField()),
                ("failed_attempts", models.PositiveIntegerField(default=0)),
                ("blocked_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["updated_at"], name="agora_auth_throttle_upd_idx")
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(bucket_hash__regex="^[0-9a-f]{64}$"),
                        name="agora_login_throttle_hash_format",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(failed_attempts__gte=0),
                        name="agora_login_throttle_attempts_nonnegative",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            sql=(USER_AUTH_VERSION_SQL,),
            reverse_sql=(USER_AUTH_VERSION_REVERSE_SQL,),
        ),
    ]
