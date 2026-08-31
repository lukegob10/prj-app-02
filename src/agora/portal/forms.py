"""Explicit, server-rendered forms for the trusted portal workflows."""

from __future__ import annotations

from typing import Any

from django import forms

from agora.core.names import InvalidSoeid, canonicalize_soeid

_PASSWORD_MAX_LENGTH = 256


class MultipleFileInput(forms.ClearableFileInput):
    """Native file input that deliberately accepts multiple attachments."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Validate each uploaded attachment through Django's normal file field."""

    def clean(self, data: Any, initial: Any = None) -> list[Any]:
        if not data:
            return []
        values = data if isinstance(data, (list, tuple)) else [data]
        clean_one = super().clean
        return [clean_one(value, initial) for value in values]


class ProjectForm(forms.Form):
    """Safe project metadata; ownership and lifecycle are never browser-controlled."""

    name = forms.CharField(
        label="Project name",
        max_length=200,
        strip=True,
        help_text="Use a clear name your viewers will recognize.",
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "off",
                "autofocus": True,
            }
        ),
    )
    description = forms.CharField(
        label="Description",
        max_length=4_000,
        required=False,
        strip=True,
        help_text="Optional context about the dashboard, source, or reporting period.",
        widget=forms.Textarea(attrs={"class": "portal-textarea", "rows": 5}),
    )


class ProjectRenameForm(forms.Form):
    """Owner-controlled dashboard display name."""

    name = forms.CharField(
        label="Dashboard name",
        max_length=200,
        strip=True,
        help_text="Names do not need to be unique; other dashboards can use the same name.",
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "off",
                "aria-describedby": "dashboard-name-help",
                "autofocus": True,
            }
        ),
    )


class GrantViewerForm(forms.Form):
    """Accept one canonical SOEID for a project-scoped Viewer grant."""

    soeid = forms.CharField(
        label="Viewer SOEID",
        max_length=64,
        strip=False,
        help_text=(
            "Enter the canonical SOEID of an active account. Owners already have Full control."
        ),
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "off",
                "autocapitalize": "characters",
                "aria-describedby": "viewer-soeid-help",
                "spellcheck": "false",
            }
        ),
    )

    def clean_soeid(self) -> str:
        try:
            return canonicalize_soeid(self.cleaned_data["soeid"])
        except InvalidSoeid as error:
            raise forms.ValidationError("Enter a valid canonical SOEID.") from error


class UserSearchForm(forms.Form):
    """Narrow the administrator list by an indexed canonical SOEID prefix."""

    query = forms.CharField(
        label="Find a user",
        max_length=64,
        required=False,
        strip=False,
        help_text="Enter the beginning of a SOEID.",
        widget=forms.TextInput(
            attrs={
                "class": "portal-input",
                "autocomplete": "off",
                "autocapitalize": "characters",
                "spellcheck": "false",
            }
        ),
    )

    def clean_query(self) -> str:
        candidate = self.cleaned_data["query"]
        if not candidate.strip(" \t\r\n\f\v"):
            return ""
        try:
            return canonicalize_soeid(candidate)
        except InvalidSoeid as error:
            raise forms.ValidationError("Enter the beginning of a valid SOEID.") from error


class RevisionUploadForm(forms.Form):
    """One flat dashboard package selected through the native multi-file queue."""

    package_files = MultipleFileField(
        label="Dashboard package files",
        required=False,
        help_text=(
            "Choose exactly one .html entry point and up to 50 supporting .csv, .css, image, "
            "or web-font files. Each file may be up to 25 MB; combined upload limit: 100 MB."
        ),
        widget=MultipleFileInput(
            attrs={
                "class": "portal-dropzone__input",
                "accept": (
                    ".html,.csv,.css,.png,.jpg,.jpeg,.gif,.webp,.woff,.woff2,"
                    "text/html,text/csv,text/css,image/png,image/jpeg,image/gif,image/webp,"
                    "font/woff,font/woff2"
                ),
                "data-upload-input": "",
                "aria-describedby": "upload-files-help upload-queue-summary",
                "required": True,
            }
        ),
    )

    def clean(self) -> dict[str, object]:
        """Keep the new queue required while accepting former multipart field names."""

        cleaned = super().clean() or {}
        has_current_files = bool(self.files.getlist("package_files") or self.files.getlist("files"))
        has_legacy_files = any(
            self.files.getlist(field) for field in ("html_file", "supporting_files", "csv_files")
        )
        if not has_current_files and not has_legacy_files and "package_files" not in self.errors:
            self.add_error("package_files", "Choose the files for this dashboard package.")
        return cleaned


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
                "autofocus": True,
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
