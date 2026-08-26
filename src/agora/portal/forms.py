"""Explicit, server-rendered forms for the portal identity workflows."""

from __future__ import annotations

from django import forms

from agora.persistence.names import InvalidSoeid, canonicalize_soeid

_PASSWORD_MAX_LENGTH = 256


class LoginForm(forms.Form):
    """Login input; credential errors are deliberately handled by the view."""

    soeid = forms.CharField(
        label="SOEID",
        max_length=64,
        strip=False,
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "username",
                "autocapitalize": "characters",
                "spellcheck": "false",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        max_length=_PASSWORD_MAX_LENGTH,
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "portal-input", "autocomplete": "current-password"}
        ),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class ProvisionUserForm(forms.Form):
    """Administrator-only account provisioning fields."""

    soeid = forms.CharField(
        label="SOEID",
        max_length=64,
        strip=False,
        help_text="ASCII letters and numbers, plus period, underscore, or hyphen.",
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "username",
                "autocapitalize": "characters",
                "spellcheck": "false",
            }
        ),
    )
    password = forms.CharField(
        label="Initial password",
        max_length=_PASSWORD_MAX_LENGTH,
        strip=False,
        help_text="Use a unique passphrase of at least 12 characters.",
        widget=forms.PasswordInput(attrs={"class": "portal-input", "autocomplete": "new-password"}),
    )
    password_confirmation = forms.CharField(
        label="Confirm initial password",
        max_length=_PASSWORD_MAX_LENGTH,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "portal-input", "autocomplete": "new-password"}),
    )
    is_administrator = forms.BooleanField(
        label="Administrator account",
        required=False,
        help_text="Administrators can provision and manage user accounts.",
    )

    def clean_soeid(self) -> str:
        try:
            return canonicalize_soeid(self.cleaned_data["soeid"])
        except InvalidSoeid as error:
            raise forms.ValidationError("Enter a valid canonical SOEID.") from error

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        password = cleaned.get("password")
        confirmation = cleaned.get("password_confirmation")
        if password and confirmation and password != confirmation:
            self.add_error("password_confirmation", "The passwords do not match.")
        return cleaned


class ResetPasswordForm(forms.Form):
    """Administrator-provided password replacement fields."""

    password = forms.CharField(
        label="New password",
        max_length=_PASSWORD_MAX_LENGTH,
        strip=False,
        help_text="Use a unique passphrase of at least 12 characters.",
        widget=forms.PasswordInput(attrs={"class": "portal-input", "autocomplete": "new-password"}),
    )
    password_confirmation = forms.CharField(
        label="Confirm new password",
        max_length=_PASSWORD_MAX_LENGTH,
        strip=False,
        widget=forms.PasswordInput(attrs={"class": "portal-input", "autocomplete": "new-password"}),
    )

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        password = cleaned.get("password")
        confirmation = cleaned.get("password_confirmation")
        if password and confirmation and password != confirmation:
            self.add_error("password_confirmation", "The passwords do not match.")
        return cleaned


class ConfirmActionForm(forms.Form):
    """Require an explicit acknowledgement before a destructive account action."""

    confirm = forms.BooleanField(
        label="I understand this will end the user's active sessions.",
        required=True,
    )
