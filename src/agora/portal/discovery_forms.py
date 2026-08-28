"""Small, server-rendered forms for dashboard discovery and tags."""

from __future__ import annotations

import unicodedata
from typing import Final

from django import forms

from agora.persistence.enhancements import MAX_EFFECTIVE_TAGS
from agora.persistence.names import InvalidDashboardTag, normalize_dashboard_tag

_SEARCH_MAX_LENGTH: Final = 200
_TAG_FIELD_NAMES: Final = tuple(f"tag_{slot}" for slot in range(1, MAX_EFFECTIVE_TAGS + 1))


class DashboardSearchForm(forms.Form):
    """Validate one plain-language prefix query without choosing its search lane."""

    query = forms.CharField(
        label="Search dashboards",
        max_length=_SEARCH_MAX_LENGTH,
        required=False,
        strip=False,
        widget=forms.SearchInput(
            attrs={
                "class": "portal-input portal-search__input",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
    )

    def clean_query(self) -> str:
        """Return compatibility-normalized text with predictable whitespace."""
        compatible = unicodedata.normalize("NFKC", self.cleaned_data["query"])
        if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in compatible):
            raise forms.ValidationError("Search cannot contain control characters.")
        normalized = " ".join(compatible.split())
        if len(normalized) > _SEARCH_MAX_LENGTH:
            raise forms.ValidationError("Search must contain 200 characters or fewer.")
        return normalized


class DashboardTagsForm(forms.Form):
    """Collect no more than five display tags while leaving persistence authoritative."""

    tag_1 = forms.CharField(
        label="Tag",
        max_length=40,
        required=False,
        strip=False,
        widget=forms.TextInput(
            attrs={"class": "portal-input", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    tag_2 = forms.CharField(
        label="Tag",
        max_length=40,
        required=False,
        strip=False,
        widget=forms.TextInput(
            attrs={"class": "portal-input", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    tag_3 = forms.CharField(
        label="Tag",
        max_length=40,
        required=False,
        strip=False,
        widget=forms.TextInput(
            attrs={"class": "portal-input", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    tag_4 = forms.CharField(
        label="Tag",
        max_length=40,
        required=False,
        strip=False,
        widget=forms.TextInput(
            attrs={"class": "portal-input", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    tag_5 = forms.CharField(
        label="Tag",
        max_length=40,
        required=False,
        strip=False,
        widget=forms.TextInput(
            attrs={"class": "portal-input", "autocomplete": "off", "spellcheck": "false"}
        ),
    )

    def clean(self) -> dict[str, object]:
        """Normalize each nonblank tag and attach duplicate errors to the later field."""
        cleaned = super().clean() or {}
        seen: dict[str, str] = {}
        for field_name in _TAG_FIELD_NAMES:
            value = cleaned.get(field_name)
            if not isinstance(value, str) or not value.strip():
                cleaned[field_name] = ""
                continue
            try:
                normalized = normalize_dashboard_tag(value)
            except InvalidDashboardTag as error:
                self.add_error(field_name, str(error))
                continue
            first_field = seen.get(normalized.key)
            if first_field is not None:
                self.add_error(
                    field_name,
                    "Enter a different tag; this matches another tag after normalization.",
                )
                continue
            seen[normalized.key] = field_name
            cleaned[field_name] = normalized.display
        return cleaned

    @property
    def labels(self) -> tuple[str, ...]:
        """Return normalized, nonblank labels after successful validation."""
        if not self.is_valid():
            raise ValueError("labels are unavailable until the tag form is valid")
        return tuple(
            value
            for field_name in _TAG_FIELD_NAMES
            if isinstance((value := self.cleaned_data[field_name]), str) and value
        )


__all__ = ["DashboardSearchForm", "DashboardTagsForm"]
