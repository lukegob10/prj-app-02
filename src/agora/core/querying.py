"""Small cross-database query helpers for exact-cardinality lookups."""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model, QuerySet

from agora.core.models import User


def get_one_or_none[ModelT: Model](queryset: QuerySet[ModelT]) -> ModelT | None:
    """Return an exact match without adding a LIMIT to a locking query."""

    try:
        return queryset.get()
    except ObjectDoesNotExist:
        return None


def administrator_user_list(*, soeid_prefix: str = "") -> QuerySet[User]:
    """Return the administrator identity listing, optionally by canonical SOEID prefix."""

    users = User.objects.only(
        "id",
        "soeid",
        "password",
        "is_active",
        "is_administrator",
    )
    if soeid_prefix:
        users = users.filter(soeid__startswith=soeid_prefix)
    return users.order_by("soeid")


__all__ = ["administrator_user_list", "get_one_or_none"]
