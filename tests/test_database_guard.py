from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.test import SimpleTestCase, TransactionTestCase

from tests import conftest as test_conftest
from tests.database_guard import (
    DATABASE_SELECTION_ATTRIBUTE,
    TEST_DATABASE_RESET_ALLOWED_ENV,
    database_tests_selected,
    item_requests_database,
    parse_test_database_reset_allowed,
)


@dataclass(slots=True)
class _FakeItem:
    fixturenames: list[str]
    has_database_marker: bool = False
    cls: type[object] | None = None

    def get_closest_marker(self, name: str) -> object | None:
        if name == "django_db" and self.has_database_marker:
            return object()
        return None


def _item(
    fixturenames: list[str],
    *,
    has_database_marker: bool = False,
    cls: type[object] | None = None,
) -> pytest.Item:
    return cast(
        pytest.Item,
        _FakeItem(
            fixturenames,
            has_database_marker=has_database_marker,
            cls=cls,
        ),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("False", False),
        ("invalid", False),
        (" true", False),
        ("TRUE", False),
        ("true", True),
    ],
)
def test_database_reset_acknowledgement_requires_exact_true(
    value: str | None,
    expected: bool,
) -> None:
    environ = {} if value is None else {TEST_DATABASE_RESET_ALLOWED_ENV: value}

    assert parse_test_database_reset_allowed(environ) is expected


@pytest.mark.parametrize(
    "fixture_name",
    [
        "db",
        "transactional_db",
        "django_db_setup",
        "django_db_reset_sequences",
        "django_db_serialized_rollback",
        "django_user_model",
        "django_username_field",
    ],
)
def test_database_selection_detects_database_fixtures(fixture_name: str) -> None:
    assert item_requests_database(_item([fixture_name])) is True


def test_database_selection_detects_django_db_marker() -> None:
    assert item_requests_database(_item([], has_database_marker=True)) is True


@pytest.mark.parametrize("fixture_names", [[], ["client"], ["django_assert_num_queries"]])
def test_database_selection_ignores_non_database_fixtures(fixture_names: list[str]) -> None:
    assert item_requests_database(_item(fixture_names)) is False
    assert database_tests_selected([_item(fixture_names)]) is False


def test_database_selection_detects_transaction_test_case() -> None:
    assert item_requests_database(_item([], cls=TransactionTestCase)) is True
    assert item_requests_database(_item([], cls=SimpleTestCase)) is False


@pytest.mark.parametrize(
    ("environment", "acknowledgement"),
    [
        ("development", "true"),
        ("test", None),
        ("test", "false"),
    ],
)
def test_collection_rejects_database_tests_without_both_safety_conditions(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    acknowledgement: str | None,
) -> None:
    monkeypatch.setenv("AGORA_ENVIRONMENT", environment)
    if acknowledgement is None:
        monkeypatch.delenv(TEST_DATABASE_RESET_ALLOWED_ENV, raising=False)
    else:
        monkeypatch.setenv(TEST_DATABASE_RESET_ALLOWED_ENV, acknowledgement)
    config = cast(pytest.Config, SimpleNamespace())

    with pytest.raises(pytest.UsageError, match="AGORA_ENVIRONMENT=test"):
        test_conftest.pytest_collection_modifyitems(config, [_item([], has_database_marker=True)])

    assert getattr(config, DATABASE_SELECTION_ATTRIBUTE) is True


def test_acknowledged_database_fixture_sets_up_and_flushes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGORA_ENVIRONMENT", "test")
    monkeypatch.setenv(TEST_DATABASE_RESET_ALLOWED_ENV, "true")
    monkeypatch.setattr(settings, "AGORA_ENVIRONMENT", "test", raising=False)
    events: list[str] = []
    database_blocker = Mock()

    @contextmanager
    def unblock_database() -> Iterator[None]:
        events.append("unblock")
        yield
        events.append("reblock")

    database_blocker.unblock.side_effect = unblock_database

    def fixture(name: str) -> object | None:
        events.append(f"fixture:{name}")
        return database_blocker if name == "django_db_blocker" else None

    get_fixture = Mock(side_effect=fixture)
    flush = Mock()
    flush.side_effect = lambda *args, **kwargs: events.append("flush")
    monkeypatch.setattr("django.core.management.call_command", flush)
    request = cast(
        pytest.FixtureRequest,
        SimpleNamespace(
            config=SimpleNamespace(**{DATABASE_SELECTION_ATTRIBUTE: True}),
            getfixturevalue=get_fixture,
        ),
    )

    prepare = cast(
        Callable[[pytest.FixtureRequest], Iterator[None]],
        cast(Any, test_conftest._prepare_shared_test_database).__wrapped__,
    )
    prepared = prepare(request)
    next(prepared)
    with pytest.raises(StopIteration):
        next(prepared)

    assert [called.args for called in get_fixture.call_args_list] == [
        ("django_db_setup",),
        ("django_db_blocker",),
    ]
    database_blocker.unblock.assert_called_once_with()
    flush.assert_called_once_with("flush", interactive=False, verbosity=0)
    assert events == [
        "fixture:django_db_setup",
        "fixture:django_db_blocker",
        "unblock",
        "flush",
        "reblock",
    ]
