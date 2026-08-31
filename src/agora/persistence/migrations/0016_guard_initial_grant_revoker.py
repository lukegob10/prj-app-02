"""Require the current owner for revocations present on new grant rows."""

from importlib import import_module
from typing import cast

from django.db import migrations

_UUID_FIX_MIGRATION = import_module(
    "agora.persistence.migrations.0015_fix_oracle_uuid_trigger_types"
)
ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL = cast(
    str,
    _UUID_FIX_MIGRATION.GRANT_AUTHORIZATION_GUARD_SQL,
)

_ORIGINAL_INSERT_GUARD = """    IF INSERTING THEN
        IF current_owner_active <> 1
           OR :NEW.created_by_id <> current_owner
           OR :NEW.viewer_id = current_owner THEN
            raise_application_error(-20006, 'grant creator must be the current owner');
        END IF;
        RETURN;
    END IF;"""

_CORRECTED_INSERT_GUARD = """    IF INSERTING THEN
        IF current_owner_active <> 1
           OR :NEW.created_by_id <> current_owner
           OR :NEW.viewer_id = current_owner THEN
            raise_application_error(-20006, 'grant creator must be the current owner');
        END IF;
        IF (:NEW.revoked_at IS NULL AND :NEW.revoked_by_id IS NOT NULL)
           OR (:NEW.revoked_at IS NOT NULL AND :NEW.revoked_by_id IS NULL) THEN
            raise_application_error(-20006, 'grant revocation fields do not match');
        END IF;
        IF :NEW.revoked_by_id IS NOT NULL
           AND :NEW.revoked_by_id <> current_owner THEN
            raise_application_error(-20006, 'grant revoker must be the current owner');
        END IF;
        RETURN;
    END IF;"""

if ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL.count(_ORIGINAL_INSERT_GUARD) != 1:
    raise RuntimeError("expected exactly one viewer-grant insert guard")

GRANT_AUTHORIZATION_GUARD_SQL = ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL.replace(
    _ORIGINAL_INSERT_GUARD,
    _CORRECTED_INSERT_GUARD,
)


class Migration(migrations.Migration):
    dependencies = [
        ("persistence", "0015_fix_oracle_uuid_trigger_types"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(GRANT_AUTHORIZATION_GUARD_SQL,),
            reverse_sql=(ORIGINAL_GRANT_AUTHORIZATION_GUARD_SQL,),
        ),
    ]
