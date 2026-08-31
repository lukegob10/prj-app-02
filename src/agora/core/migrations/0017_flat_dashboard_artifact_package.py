"""Allow immutable revisions to carry the flat dashboard supporting-artifact package."""

from django.db import migrations, models

ARTIFACT_KINDS = ["html", "csv", "css", "img", "font"]
ARTIFACT_MEDIA_TYPE_CONDITION = (
    models.Q(kind="html", media_type="text/html")
    | models.Q(kind="csv", media_type="text/csv")
    | models.Q(kind="css", media_type="text/css")
    | models.Q(
        kind="img",
        media_type__in=["image/png", "image/jpeg", "image/gif", "image/webp"],
    )
    | models.Q(kind="font", media_type__in=["font/woff", "font/woff2"])
)

# Some long-lived Oracle schemas were bootstrapped before the two named kind checks were
# present (the other append-only guards and the one-HTML function index are still authoritative
# there).  Drop by name when present, but make the forward migration safe for either schema
# shape.  The state operations below still accurately replace the historical checks.
DROP_OLD_ARTIFACT_CHECKS_SQL = (
    """DECLARE
        constraint_count NUMBER;
    BEGIN
        SELECT COUNT(*)
        INTO constraint_count
        FROM user_constraints
        WHERE table_name = 'TB_TA_AGORA_ARTIFACT'
          AND constraint_name = 'AGORA_ARTIFACT_VALID_KIND';
        IF constraint_count = 1 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE TB_TA_AGORA_ARTIFACT DROP CONSTRAINT AGORA_ARTIFACT_VALID_KIND';
        END IF;
    END;;""",
    """DECLARE
        constraint_count NUMBER;
    BEGIN
        SELECT COUNT(*)
        INTO constraint_count
        FROM user_constraints
        WHERE table_name = 'TB_TA_AGORA_ARTIFACT'
          AND constraint_name = 'AGORA_ARTIFACT_KIND_MEDIA_TYPE';
        IF constraint_count = 1 THEN
            EXECUTE IMMEDIATE
                'ALTER TABLE TB_TA_AGORA_ARTIFACT DROP CONSTRAINT AGORA_ARTIFACT_KIND_MEDIA_TYPE';
        END IF;
    END;;""",
)

RESTORE_OLD_ARTIFACT_CHECKS_SQL = (
    """
    ALTER TABLE TB_TA_AGORA_ARTIFACT
    ADD CONSTRAINT AGORA_ARTIFACT_VALID_KIND
    CHECK (kind IN ('html', 'csv'))
    """,
    """
    ALTER TABLE TB_TA_AGORA_ARTIFACT
    ADD CONSTRAINT AGORA_ARTIFACT_KIND_MEDIA_TYPE
    CHECK (
        (kind = 'html' AND media_type = 'text/html')
        OR (kind = 'csv' AND media_type = 'text/csv')
    )
    """,
)


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0016_guard_initial_grant_revoker"),
    ]

    operations = [
        # Replace both old checks.  The existing one-HTML function-based unique index and all
        # append-only triggers remain in place and continue to enforce their independent
        # invariants; the compact ``img`` database value fits the existing VARCHAR2(4).
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_OLD_ARTIFACT_CHECKS_SQL,
                    reverse_sql=RESTORE_OLD_ARTIFACT_CHECKS_SQL,
                ),
                migrations.AddConstraint(
                    model_name="artifact",
                    constraint=models.CheckConstraint(
                        condition=models.Q(kind__in=ARTIFACT_KINDS),
                        name="agora_artifact_valid_kind",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="artifact",
                    constraint=models.CheckConstraint(
                        condition=ARTIFACT_MEDIA_TYPE_CONDITION,
                        name="agora_artifact_kind_media_type",
                    ),
                ),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="artifact",
                    name="agora_artifact_valid_kind",
                ),
                migrations.RemoveConstraint(
                    model_name="artifact",
                    name="agora_artifact_kind_media_type",
                ),
                # Keep the migration state in sync with the model choices without issuing an
                # Oracle ALTER COLUMN (the compact ``img`` value fits the existing width).
                migrations.AlterField(
                    model_name="artifact",
                    name="kind",
                    field=models.CharField(
                        choices=[
                            ("html", "HTML"),
                            ("csv", "CSV"),
                            ("css", "CSS"),
                            ("img", "Image"),
                            ("font", "Font"),
                        ],
                        max_length=4,
                    ),
                ),
                migrations.AddConstraint(
                    model_name="artifact",
                    constraint=models.CheckConstraint(
                        condition=models.Q(kind__in=ARTIFACT_KINDS),
                        name="agora_artifact_valid_kind",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="artifact",
                    constraint=models.CheckConstraint(
                        condition=ARTIFACT_MEDIA_TYPE_CONDITION,
                        name="agora_artifact_kind_media_type",
                    ),
                ),
            ],
        ),
    ]
