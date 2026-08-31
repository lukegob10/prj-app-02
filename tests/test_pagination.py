"""Dependency-light coverage for signed, bounded keyset pagination."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from django.core import signing
from django.db.models import Model, Q, QuerySet

from agora.core import pagination
from agora.core.models import Dashboard, User
from agora.core.pagination import (
    CursorColumn,
    CursorValueKind,
    InvalidCursor,
    paginate_keyset,
)
from agora.portal.forms import UserSearchForm

USER_COLUMNS = (CursorColumn("soeid", CursorValueKind.TEXT),)


def _query_returning[ModelT: Model](
    *rows: ModelT, after_filter: bool = False
) -> tuple[QuerySet[ModelT], MagicMock]:
    queryset = MagicMock()
    ordered_source = queryset.filter.return_value if after_filter else queryset
    ordered_source.order_by.return_value.__getitem__.return_value = list(rows)
    return cast(QuerySet[ModelT], queryset), queryset


def test_pagination_rejects_every_invalid_server_and_cursor_shape() -> None:
    with pytest.raises(ValueError, match="public model field"):
        CursorColumn("", CursorValueKind.TEXT)
    with pytest.raises(ValueError, match="public model field"):
        CursorColumn("_private", CursorValueKind.TEXT)
    with pytest.raises(ValueError, match="public model field paths"):
        CursorColumn("owner____soeid", CursorValueKind.TEXT)

    queryset = User.objects.none()
    with pytest.raises(ValueError, match="at least one"):
        paginate_keyset(queryset, columns=(), namespace="users")
    columns = (CursorColumn("soeid", CursorValueKind.TEXT),)
    with pytest.raises(ValueError, match="between 1 and 100"):
        paginate_keyset(queryset, columns=columns, namespace="users", page_size=0)
    with pytest.raises(ValueError, match="namespace"):
        paginate_keyset(queryset, columns=columns, namespace="")

    salt = pagination._cursor_salt("users", "")
    malformed_payloads = (
        signing.dumps([], salt=salt),
        signing.dumps({"v": 1, "d": "next", "wrong": []}, salt=salt),
        signing.dumps({"v": 2, "d": "next", "p": ["A"]}, salt=salt),
        signing.dumps({"v": 1, "d": "sideways", "p": ["A"]}, salt=salt),
        signing.dumps({"v": 1, "d": "next", "p": "A"}, salt=salt),
        signing.dumps({"v": 1, "d": "next", "p": []}, salt=salt),
        signing.dumps({"v": 1, "d": "next", "p": [1]}, salt=salt),
    )
    with pytest.raises(InvalidCursor):
        pagination._decode_cursor(
            1,  # type: ignore[arg-type]
            columns=columns,
            namespace="users",
            context="",
            max_age=60,
        )
    for cursor in malformed_payloads:
        with pytest.raises(InvalidCursor):
            pagination._decode_cursor(
                cursor,
                columns=columns,
                namespace="users",
                context="",
                max_age=60,
            )

    with pytest.raises(TypeError, match="encoded cursor"):
        pagination._decode_value(1, CursorValueKind.UUID)
    with pytest.raises(TypeError, match="unsupported"):
        pagination._decode_value("value", "unsupported")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boundary"):
        pagination._validated_value(True, CursorValueKind.INTEGER)


def test_user_search_accepts_only_a_canonicalizable_soeid_prefix() -> None:
    normalized = UserSearchForm({"query": "  user.team  "})
    assert normalized.is_valid()
    assert normalized.cleaned_data["query"] == "USER.TEAM"

    invalid = UserSearchForm({"query": "\N{EM SPACE}"})
    assert not invalid.is_valid()
    assert "query" in invalid.errors


def test_keyset_page_fetches_only_one_sentinel_and_builds_safe_urls() -> None:
    rows = tuple(User(soeid=f"USER.{number:02d}") for number in range(26))
    queryset, queryset_mock = _query_returning(*rows)

    page = paginate_keyset(
        queryset,
        columns=USER_COLUMNS,
        namespace="test-users",
        context="administrator-a",
    ).with_urls(
        base_url="/admin/users/",
        cursor_parameter="cursor",
        preserved_query={"query": "USER."},
    )

    assert len(page) == 25
    assert [user.soeid for user in page] == [f"USER.{number:02d}" for number in range(25)]
    assert page.previous_url is None
    assert page.next_url is not None
    assert page.next_url.startswith("/admin/users/?query=USER.&cursor=")
    queryset_mock.order_by.return_value.__getitem__.assert_called_once_with(slice(None, 26, None))
    queryset_mock.order_by.assert_called_once_with("soeid")


def test_cursor_is_signed_scoped_and_cannot_supply_ordering() -> None:
    first_query, _ = _query_returning(User(soeid="A"), User(soeid="B"))
    first = paginate_keyset(
        first_query,
        columns=USER_COLUMNS,
        namespace="test-users",
        context="administrator-a",
        page_size=1,
    )
    assert first.next_cursor is not None

    tampered = f"{first.next_cursor[:-1]}{'A' if first.next_cursor[-1] != 'A' else 'B'}"
    with pytest.raises(InvalidCursor):
        paginate_keyset(
            _query_returning(User(soeid="B"), after_filter=True)[0],
            columns=USER_COLUMNS,
            namespace="test-users",
            context="administrator-a",
            cursor=tampered,
            page_size=1,
        )

    with pytest.raises(InvalidCursor):
        paginate_keyset(
            _query_returning(User(soeid="B"), after_filter=True)[0],
            columns=USER_COLUMNS,
            namespace="test-users",
            context="administrator-b",
            cursor=first.next_cursor,
            page_size=1,
        )


def test_previous_cursor_reverses_database_order_but_returns_canonical_rows() -> None:
    first = paginate_keyset(
        _query_returning(User(soeid="A"), User(soeid="B"))[0],
        columns=USER_COLUMNS,
        namespace="test-users",
        page_size=1,
    )
    assert first.next_cursor is not None

    second_query, _ = _query_returning(User(soeid="B"), after_filter=True)
    second = paginate_keyset(
        second_query,
        columns=USER_COLUMNS,
        namespace="test-users",
        cursor=first.next_cursor,
        page_size=1,
    )
    assert [row.soeid for row in second] == ["B"]
    assert second.previous_cursor is not None

    previous_query, previous_query_mock = _query_returning(User(soeid="A"), after_filter=True)
    previous = paginate_keyset(
        previous_query,
        columns=USER_COLUMNS,
        namespace="test-users",
        cursor=second.previous_cursor,
        page_size=1,
    )
    assert [row.soeid for row in previous] == ["A"]
    previous_query_mock.filter.return_value.order_by.assert_called_once_with("-soeid")


def test_empty_page_after_record_removal_retains_a_safe_way_back() -> None:
    first = paginate_keyset(
        _query_returning(User(soeid="A"), User(soeid="B"))[0],
        columns=USER_COLUMNS,
        namespace="test-users",
        page_size=1,
    )
    assert first.next_cursor is not None

    empty_queryset: QuerySet[User] = _query_returning(after_filter=True)[0]
    empty = paginate_keyset(
        empty_queryset,
        columns=USER_COLUMNS,
        namespace="test-users",
        cursor=first.next_cursor,
        page_size=1,
    ).with_urls(
        base_url="/admin/users/",
        cursor_parameter="cursor",
        preserved_query={"query": "USER."},
    )
    assert not empty
    assert empty.previous_cursor is not None
    assert empty.previous_url == "/admin/users/?query=USER."
    assert empty.next_cursor is None


def test_mixed_direction_boundary_is_lexicographic_and_server_controlled() -> None:
    updated_at = datetime(2026, 8, 28, tzinfo=UTC)
    first_row = Dashboard(id=UUID(int=1), updated_at=updated_at)
    second_row = Dashboard(id=UUID(int=2), updated_at=updated_at)
    columns = (
        CursorColumn("updated_at", CursorValueKind.DATETIME, descending=True),
        CursorColumn("id", CursorValueKind.UUID),
    )
    first_query, _ = _query_returning(first_row, second_row)
    first = paginate_keyset(
        first_query,
        columns=columns,
        namespace="test-projects",
        page_size=1,
    )
    assert first.next_cursor is not None

    next_query, next_query_mock = _query_returning(second_row, after_filter=True)
    next_page = paginate_keyset(
        next_query,
        columns=columns,
        namespace="test-projects",
        cursor=first.next_cursor,
        page_size=1,
    )

    expected = Q(updated_at__lt=updated_at) | (Q(updated_at=updated_at) & Q(id__gt=first_row.id))
    next_query_mock.filter.assert_called_once_with(expected)
    next_query_mock.filter.return_value.order_by.assert_called_once_with("-updated_at", "id")
    assert [row.id for row in next_page] == [second_row.id]
