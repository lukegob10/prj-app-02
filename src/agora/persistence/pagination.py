"""Typed, signed keyset pagination for bounded portal read paths."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from django.core import signing
from django.db.models import Model, Q, QuerySet

DEFAULT_PAGE_SIZE = 25
_CURSOR_MAX_AGE_SECONDS = 86_400
_CURSOR_MAX_LENGTH = 2_048
_CURSOR_VERSION = 1

type CursorValue = str | int | datetime | UUID
type EncodedCursorValue = str | int


class InvalidCursor(ValueError):
    """Raised when a cursor is malformed, expired, or valid for another read path."""


class CursorValueKind(StrEnum):
    """Supported boundary value types for signed keyset cursors."""

    TEXT = "text"
    INTEGER = "integer"
    DATETIME = "datetime"
    UUID = "uuid"


@dataclass(frozen=True, slots=True)
class CursorColumn:
    """One fixed server-controlled ordering column in a keyset."""

    field: str
    kind: CursorValueKind
    descending: bool = False

    def __post_init__(self) -> None:
        if not self.field or self.field.startswith("_"):
            raise ValueError("cursor columns require a public model field")
        if any(not part or part.startswith("_") for part in self.field.split("__")):
            raise ValueError("cursor columns require public model field paths")


@dataclass(frozen=True, slots=True)
class CursorPage[ModelT: Model]:
    """One bounded page plus opaque navigation generated from its boundaries."""

    items: tuple[ModelT, ...]
    previous_cursor: str | None
    next_cursor: str | None
    previous_url: str | None = None
    next_url: str | None = None

    def __iter__(self) -> Iterator[ModelT]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def with_urls(
        self,
        *,
        base_url: str,
        cursor_parameter: str,
        preserved_query: Mapping[str, str] | None = None,
    ) -> CursorPage[ModelT]:
        """Attach navigation URLs built only from validated server-side state."""

        query = dict(preserved_query or {})
        previous_url = _cursor_url(
            base_url,
            cursor_parameter=cursor_parameter,
            cursor=self.previous_cursor,
            preserved_query=query,
        )
        next_url = _cursor_url(
            base_url,
            cursor_parameter=cursor_parameter,
            cursor=self.next_cursor,
            preserved_query=query,
        )
        if not self.items:
            # If concurrent deletes/revokes empty a cursor page, reset the available recovery
            # link to the first page. This avoids trapping the user between two empty boundaries.
            reset_url = _query_url(base_url, query)
            previous_url = reset_url if previous_url is not None else None
            next_url = reset_url if next_url is not None else None
        return replace(
            self,
            previous_url=previous_url,
            next_url=next_url,
        )


def paginate_keyset[ModelT: Model](
    queryset: QuerySet[ModelT],
    *,
    columns: tuple[CursorColumn, ...],
    namespace: str,
    cursor: str | None = None,
    context: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    max_age: int = _CURSOR_MAX_AGE_SECONDS,
) -> CursorPage[ModelT]:
    """Fetch one deterministic keyset page with at most one sentinel row.

    Ordering fields and directions are supplied by application code, never by the cursor. The
    signed cursor contains only the fixed ordering boundary and navigation direction, and its
    signature is scoped to the read-path namespace and immutable filter context.
    """

    if not columns:
        raise ValueError("keyset pagination requires at least one ordering column")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if not namespace or len(namespace) > 100:
        raise ValueError("pagination namespace must be between 1 and 100 characters")

    decoded = (
        _decode_cursor(
            cursor,
            columns=columns,
            namespace=namespace,
            context=context,
            max_age=max_age,
        )
        if cursor
        else None
    )
    direction = decoded[0] if decoded is not None else "next"
    boundary = decoded[1] if decoded is not None else None

    page_queryset = queryset
    if boundary is not None:
        page_queryset = page_queryset.filter(
            _boundary_filter(columns, boundary, previous=direction == "previous")
        )

    ordering = _ordering(columns, reverse=direction == "previous")
    fetched = list(page_queryset.order_by(*ordering)[: page_size + 1])
    has_sentinel = len(fetched) > page_size
    items = fetched[:page_size]
    if direction == "previous":
        items.reverse()

    has_previous = (decoded is not None and direction == "next") or (
        direction == "previous" and has_sentinel
    )
    has_next = (direction == "next" and has_sentinel) or (
        decoded is not None and direction == "previous"
    )

    previous_boundary: tuple[CursorValue, ...] | None
    next_boundary: tuple[CursorValue, ...] | None
    if items:
        previous_boundary = _model_boundary(items[0], columns)
        next_boundary = _model_boundary(items[-1], columns)
    else:
        # Rows can be deleted, disabled, or revoked after a cursor is issued. Retain a safe way
        # back using the validated original boundary without depending on that row still existing.
        previous_boundary = boundary
        next_boundary = boundary

    previous_cursor = (
        _encode_cursor(
            "previous",
            previous_boundary,
            columns=columns,
            namespace=namespace,
            context=context,
        )
        if has_previous and previous_boundary is not None
        else None
    )
    next_cursor = (
        _encode_cursor(
            "next",
            next_boundary,
            columns=columns,
            namespace=namespace,
            context=context,
        )
        if has_next and next_boundary is not None
        else None
    )
    return CursorPage(
        items=tuple(items),
        previous_cursor=previous_cursor,
        next_cursor=next_cursor,
    )


def _boundary_filter(
    columns: tuple[CursorColumn, ...],
    values: tuple[CursorValue, ...],
    *,
    previous: bool,
) -> Q:
    result = Q()
    equal_prefix = Q()
    for column, value in zip(columns, values, strict=True):
        greater_than = column.descending == previous
        comparison = "gt" if greater_than else "lt"
        result |= equal_prefix & Q(**{f"{column.field}__{comparison}": value})
        equal_prefix &= Q(**{column.field: value})
    return result


def _ordering(columns: tuple[CursorColumn, ...], *, reverse: bool) -> tuple[str, ...]:
    return tuple(("-" if column.descending != reverse else "") + column.field for column in columns)


def _model_boundary[ModelT: Model](
    instance: ModelT,
    columns: tuple[CursorColumn, ...],
) -> tuple[CursorValue, ...]:
    values: list[CursorValue] = []
    for column in columns:
        value: object = instance
        for attribute in column.field.split("__"):
            value = getattr(value, attribute)
        values.append(_validated_value(value, column.kind))
    return tuple(values)


def _encode_cursor(
    direction: str,
    values: tuple[CursorValue, ...],
    *,
    columns: tuple[CursorColumn, ...],
    namespace: str,
    context: str,
) -> str:
    encoded = [
        _encode_value(value, column.kind) for column, value in zip(columns, values, strict=True)
    ]
    return signing.dumps(
        {"v": _CURSOR_VERSION, "d": direction, "p": encoded},
        salt=_cursor_salt(namespace, context),
    )


def _decode_cursor(
    cursor: str,
    *,
    columns: tuple[CursorColumn, ...],
    namespace: str,
    context: str,
    max_age: int,
) -> tuple[str, tuple[CursorValue, ...]]:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= _CURSOR_MAX_LENGTH:
        raise InvalidCursor("invalid pagination cursor")
    try:
        payload: object = signing.loads(
            cursor,
            salt=_cursor_salt(namespace, context),
            max_age=max_age,
        )
    except signing.BadSignature as error:
        raise InvalidCursor("invalid pagination cursor") from error
    if not isinstance(payload, dict) or set(payload) != {"v", "d", "p"}:
        raise InvalidCursor("invalid pagination cursor")
    if payload["v"] != _CURSOR_VERSION or payload["d"] not in {"next", "previous"}:
        raise InvalidCursor("invalid pagination cursor")
    encoded_values = payload["p"]
    if not isinstance(encoded_values, list) or len(encoded_values) != len(columns):
        raise InvalidCursor("invalid pagination cursor")
    try:
        values = tuple(
            _decode_value(value, column.kind)
            for column, value in zip(columns, encoded_values, strict=True)
        )
    except (TypeError, ValueError) as error:
        raise InvalidCursor("invalid pagination cursor") from error
    return cast(str, payload["d"]), values


def _encode_value(value: CursorValue, kind: CursorValueKind) -> EncodedCursorValue:
    value = _validated_value(value, kind)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _decode_value(value: object, kind: CursorValueKind) -> CursorValue:
    if kind is CursorValueKind.TEXT:
        return _validated_value(value, kind)
    if kind is CursorValueKind.INTEGER:
        return _validated_value(value, kind)
    if not isinstance(value, str):
        raise TypeError("encoded cursor value has the wrong type")
    if kind is CursorValueKind.DATETIME:
        return datetime.fromisoformat(value)
    if kind is CursorValueKind.UUID:
        return UUID(value)
    raise TypeError("unsupported cursor value kind")


def _validated_value(value: object, kind: CursorValueKind) -> CursorValue:
    if kind is CursorValueKind.TEXT and isinstance(value, str):
        return value
    if kind is CursorValueKind.INTEGER and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind is CursorValueKind.DATETIME and isinstance(value, datetime):
        return value
    if kind is CursorValueKind.UUID and isinstance(value, UUID):
        return value
    raise TypeError("cursor boundary has the wrong type")


def _cursor_salt(namespace: str, context: str) -> str:
    context_digest = sha256(context.encode("utf-8")).hexdigest()
    return f"agora.pagination.v1:{namespace}:{context_digest}"


def _cursor_url(
    base_url: str,
    *,
    cursor_parameter: str,
    cursor: str | None,
    preserved_query: Mapping[str, str],
) -> str | None:
    if cursor is None:
        return None
    query = {key: value for key, value in preserved_query.items() if value}
    query[cursor_parameter] = cursor
    return f"{base_url}?{urlencode(query)}"


def _query_url(base_url: str, query: Mapping[str, str]) -> str:
    encoded = urlencode({key: value for key, value in query.items() if value})
    return f"{base_url}?{encoded}" if encoded else base_url


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "CursorColumn",
    "CursorPage",
    "CursorValueKind",
    "InvalidCursor",
    "paginate_keyset",
]
