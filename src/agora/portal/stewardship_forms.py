"""Narrow forms for access requests and ownership stewardship workflows."""

from __future__ import annotations

from django import forms

from agora.persistence.enhancements import MAX_ACCESS_REQUEST_MESSAGE_LENGTH
from agora.persistence.names import InvalidSoeid, canonicalize_soeid


class DashboardAccessRequestForm(forms.Form):
    """Accept an optional owner-visible note without trusting it as identity or authority."""

    message = forms.CharField(
        label="Message (optional)",
        max_length=MAX_ACCESS_REQUEST_MESSAGE_LENGTH,
        required=False,
        strip=True,
        help_text="The dashboard owner can read this plain-text message.",
        widget=forms.Textarea(
            attrs={
                "class": "portal-textarea",
                "rows": 4,
                "maxlength": MAX_ACCESS_REQUEST_MESSAGE_LENGTH,
                "aria-describedby": "access-request-message-help",
            }
        ),
    )


class TransferOwnershipForm(forms.Form):
    """Identify one active incoming owner by canonical SOEID."""

    incoming_owner_soeid = forms.CharField(
        label="New owner SOEID",
        max_length=64,
        strip=False,
        help_text="Enter the canonical SOEID of the active account that will take Full control.",
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "off",
                "autocapitalize": "characters",
                "aria-describedby": "incoming-owner-soeid-help",
                "spellcheck": "false",
            }
        ),
    )

    def clean_incoming_owner_soeid(self) -> str:
        try:
            return canonicalize_soeid(self.cleaned_data["incoming_owner_soeid"])
        except InvalidSoeid as error:
            raise forms.ValidationError("Enter a valid canonical SOEID.") from error


class TransferOwnershipConfirmForm(forms.Form):
    """Require one explicit acknowledgement on the dedicated confirmation page."""

    confirm = forms.BooleanField(
        label=(
            "I understand that I will immediately lose Full control and that this transfer "
            "is retained in ownership history."
        ),
        required=True,
    )


__all__ = [
    "DashboardAccessRequestForm",
    "TransferOwnershipConfirmForm",
    "TransferOwnershipForm",
]
