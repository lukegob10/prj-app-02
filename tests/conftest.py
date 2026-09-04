from __future__ import annotations

import os
from collections.abc import Iterator
from typing import cast

import pytest

from tests.database_guard import (
    DATABASE_SELECTION_ATTRIBUTE,
    database_acknowledgement_error,
    database_profile_matches_acknowledgement,
    database_reset_is_explicitly_allowed,
    database_tests_selected,
)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Require an explicit disposable-schema acknowledgement before DB fixtures can start."""
    database_selected = database_tests_selected(items)
    setattr(config, DATABASE_SELECTION_ATTRIBUTE, database_selected)
    if database_selected and not database_reset_is_explicitly_allowed(os.environ):
        raise pytest.UsageError(database_acknowledgement_error())


@pytest.fixture(scope="session", autouse=True)
def _prepare_shared_test_database(request: pytest.FixtureRequest) -> Iterator[None]:
    """Set up and deterministically flush the acknowledged reusable test schema once."""
    if not getattr(request.config, DATABASE_SELECTION_ATTRIBUTE, False):
        yield
        return

    if not database_reset_is_explicitly_allowed(os.environ):
        raise pytest.UsageError(database_acknowledgement_error())

    request.getfixturevalue("django_db_setup")

    from django.conf import settings

    if getattr(settings, "AGORA_ENVIRONMENT", None) != "test":
        raise pytest.UsageError(
            "Database-backed tests require AGORA_ENVIRONMENT=test after Django setup."
        )
    database_config = settings.DATABASES.get("default", {})
    database_options = cast(dict[str, object], database_config.get("OPTIONS", {}))
    configured_profile = database_options.get("environment")
    if not database_profile_matches_acknowledgement(configured_profile, os.environ):
        raise pytest.UsageError(database_acknowledgement_error())

    from django.core.management import call_command

    database_blocker = request.getfixturevalue("django_db_blocker")
    with database_blocker.unblock():
        call_command("flush", interactive=False, verbosity=0)
    yield
