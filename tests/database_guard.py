"""Pure helpers for guarding the shared disposable test schema."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

import pytest

DATABASE_SELECTION_ATTRIBUTE: Final = "_agora_database_tests_selected"
TEST_DATABASE_RESET_ALLOWED_ENV: Final = "AGORA_TEST_DATABASE_RESET_ALLOWED"

_DATABASE_FIXTURE_NAMES: Final = frozenset(
    {
        "db",
        "transactional_db",
        "django_db_setup",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "django_user_model",
        "django_username_field",
    }
)


def database_reset_is_explicitly_allowed(environ: Mapping[str, str]) -> bool:
    """Require both opt-ins before tests may flush the configured Oracle schema."""
    return (
        environ.get("AGORA_ENVIRONMENT") == "test"
        and environ.get(TEST_DATABASE_RESET_ALLOWED_ENV) == "true"
    )


def database_acknowledgement_error() -> str:
    """Describe the destructive-test acknowledgement without exposing configuration."""
    return (
        "AGORA_ENVIRONMENT=test and "
        f"{TEST_DATABASE_RESET_ALLOWED_ENV}=true are required before database checks. "
        "Set them only when the selected Oracle schema is disposable and dedicated to "
        "Agora validation."
    )


def parse_test_database_reset_allowed(environ: Mapping[str, str]) -> bool:
    """Accept only the exact opt-in value for the destructive shared-schema reset."""
    return environ.get(TEST_DATABASE_RESET_ALLOWED_ENV) == "true"


def item_requests_database(item: pytest.Item) -> bool:
    """Return whether one collected item can cause pytest-django database access."""
    if item.get_closest_marker("django_db") is not None:
        return True

    fixture_names = getattr(item, "fixturenames", ())
    if any(fixture_name in _DATABASE_FIXTURE_NAMES for fixture_name in fixture_names):
        return True

    item_class = getattr(item, "cls", None)
    if not isinstance(item_class, type):
        return False

    from django.test import SimpleTestCase

    return issubclass(item_class, SimpleTestCase) and bool(getattr(item_class, "databases", ()))


def database_tests_selected(items: Iterable[pytest.Item]) -> bool:
    """Return whether the retained collection includes any database-backed item."""
    return any(item_requests_database(item) for item in items)
