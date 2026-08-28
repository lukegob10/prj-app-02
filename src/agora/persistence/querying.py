"""Small cross-database query helpers for exact-cardinality lookups."""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, QuerySet


def get_one_or_none[ModelT: Model](queryset: QuerySet[ModelT]) -> ModelT | None:
    """Return an exact match without adding a LIMIT to a locking query."""

    try:
        return queryset.get()
    except ObjectDoesNotExist:
        return None
