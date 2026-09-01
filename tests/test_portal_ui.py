from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.http import HttpRequest
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client
from django.utils.datastructures import MultiValueDict

from agora.portal.forms import GrantViewerForm, RevisionUploadForm, UserSearchForm
from agora.portal.views import _request_upload_parts, _upload_error_message
from agora.uploads import UploadIssue, UploadIssueCode, UploadIssueField, UploadRejected

PORTAL_CSS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agora"
    / "portal"
    / "static"
    / "portal"
    / "foundation.css"
)
UPLOAD_DROPZONE_JS = PORTAL_CSS.with_name("upload-dropzone.js")
BRAND_ROOT = PORTAL_CSS.parent / "brand"
TEMPLATE_ROOT = PORTAL_CSS.parents[2] / "templates" / "portal"


class DocumentParser(HTMLParser):
    """Capture enough structure for a lightweight server-rendered smoke check."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text_parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def attributes_for(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for element, attrs in self.elements if element == tag]

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


def render_component(name: str, context: dict[str, object]) -> str:
    template = Template(f'{{% include "portal/components/{name}.html" %}}')
    return template.render(Context(context))


@pytest.mark.smoke
def test_portal_shell_renders_accessible_landmarks_and_truthful_states(client: Client) -> None:
    response = client.get("/", HTTP_HOST="localhost")
    document = response.content.decode()
    parser = DocumentParser()
    parser.feed(document)

    assert response.status_code == 200
    assert document.lstrip().lower().startswith("<!doctype html>")
    assert parser.attributes_for("html") == [{"lang": "en"}]
    assert len(parser.attributes_for("header")) == 1
    assert len(parser.attributes_for("nav")) == 1
    assert len(parser.attributes_for("main")) == 1
    assert len(parser.attributes_for("footer")) == 1
    assert parser.attributes_for("main")[0] == {
        "id": "main-content",
        "class": "portal-main",
        "tabindex": "-1",
    }

    links = parser.attributes_for("a")
    assert {link.get("href") for link in links} >= {"#main-content", "/"}
    assert sum(link.get("href") == "/login/" for link in links) == 2
    assert any(link.get("class") == "portal-skip-link" for link in links)
    assert any(link.get("aria-current") == "page" for link in links)
    assert any(
        image.get("class") == "portal-brand__wordmark" and image.get("alt") == "Agora"
        for image in parser.attributes_for("img")
    )
    assert parser.attributes_for("nav")[0].get("aria-label") == "Primary navigation"
    assert parser.attributes_for("summary")[0].get("aria-controls") == "primary-navigation-menu"

    headings = [element for element, _ in parser.elements if element in {"h1", "h2", "h3"}]
    assert headings.count("h1") == 1
    assert headings == ["h1", "h2"]
    assert "Share dashboards without giving up control" in parser.text
    assert "Every release stays deliberate" in parser.text
    assert "Every accepted upload is retained as an immutable revision" in parser.text
    assert "Dashboard code runs outside the trusted portal" in parser.text
    assert "Only approved users can open published work" in parser.text
    assert "Administrator-provisioned access" not in parser.text
    assert (
        document.count(
            '<a class="portal-nav__link portal-nav__link--primary" href="/login/">Sign In</a>'
        )
        == 2
    )
    assert 'class="portal-public-hero"' in document
    assert 'class="portal-public-safeguards"' in document
    assert 'class="portal-public-safeguards__list"' in document
    assert "portal-public-workflow" not in document
    assert "portal-public-boundary" not in document
    assert "portal-brand__mark" not in document
    assert "recommend" not in document.lower()


def test_revision_upload_form_describes_supported_flat_package_files() -> None:
    form = RevisionUploadForm()

    assert tuple(form.fields) == ("package_files",)
    assert form.fields["package_files"].label == "Dashboard package files"
    assert "exactly one .html" in str(form.fields["package_files"].help_text)
    assert "self-contained" not in str(form.fields["package_files"].help_text).lower()

    accepted = form.fields["package_files"].widget.attrs["accept"]
    assert accepted == (
        ".html,.csv,.css,.png,.jpg,.jpeg,.gif,.webp,.woff,.woff2,"
        "text/html,text/csv,text/css,image/png,image/jpeg,image/gif,image/webp,"
        "font/woff,font/woff2"
    )
    assert form.fields["package_files"].widget.allow_multiple_selected is True
    assert form.fields["package_files"].widget.attrs["data-upload-input"] == ""
    assert form.fields["package_files"].widget.attrs["required"] is True

    assert RevisionUploadForm(data={}, files=MultiValueDict()).is_valid() is False
    legacy_form = RevisionUploadForm(
        data={},
        files=MultiValueDict(
            {"html_file": [SimpleUploadedFile("dashboard.html", b"<html></html>")]}
        ),
    )
    assert legacy_form.is_valid() is True


def test_upload_template_uses_one_dropzone_and_states_replacement_policy() -> None:
    source = (TEMPLATE_ROOT / "projects" / "upload.html").read_text(encoding="utf-8")
    base_source = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")

    assert "form.package_files" in source
    assert "form.html_file" not in source
    assert "form.supporting_files" not in source
    assert "form.csv_files" not in source
    assert "data-upload-dropzone" in source
    assert "data-upload-list" in source
    assert source.count("upload-dropzone.js") == 1
    assert source.count("{% static 'portal/upload-dropzone.js' %}") == 1
    assert source.count("defer></script>") == 1
    assert "upload-dropzone.js" not in base_source
    assert 'class="portal-upload-feedback"' in source
    assert "Drag and drop dashboard files" in source
    assert "click anywhere to choose multiple files" in source
    assert "replaces a queued file with the same name" in source
    assert "one required HTML entry point" in source
    assert "up to 50 supporting files" in source
    assert "Relative references" in source
    assert "Outside URLs are blocked" in source
    assert "separate JavaScript files are not supported" in source
    assert "Revisions are immutable" in source
    assert "self-contained" not in source.lower()


def test_upload_dropzone_script_merges_batches_without_rendering_untrusted_html() -> None:
    source = UPLOAD_DROPZONE_JS.read_text(encoding="utf-8")

    assert "new Map()" in source
    assert "new DataTransfer()" in source
    assert 'addEventListener("drop"' in source
    assert 'addEventListener("dragenter"' in source
    assert 'addEventListener("dragleave"' in source
    assert 'addEventListener("change"' in source
    assert 'addEventListener("cancel"' in source
    assert 'document.addEventListener("dragover", preventNativeFileOpen, true)' in source
    assert 'document.addEventListener("drop", preventNativeFileOpen, true)' in source
    assert 'widget.dataset.uploadReady = "true"' in source
    assert 'window.addEventListener("focus"' not in source
    assert "queuedFiles.set(key, file)" in source
    assert "dragDepth = Math.max(0, dragDepth - 1)" in source
    assert "added to the upload queue" in source
    assert "replaced the queued" in source
    assert "textContent" in source
    assert "innerHTML" not in source


def _multipart_request(
    files: Mapping[str, Sequence[UploadedFile[Any]]],
) -> HttpRequest:
    request = HttpRequest()
    request.FILES = MultiValueDict({key: list(values) for key, values in files.items()})
    return request


@pytest.mark.parametrize(
    ("files", "expected_code"),
    [
        (
            {
                "html_file": [
                    SimpleUploadedFile("one.html", b"<html></html>"),
                    SimpleUploadedFile("two.html", b"<html></html>"),
                ]
            },
            UploadIssueCode.MULTIPLE_HTML,
        ),
        (
            {
                "html_file": [SimpleUploadedFile("dashboard.html", b"<html></html>")],
                "unrecognized": [SimpleUploadedFile("hidden.bin", b"ignored bytes")],
            },
            UploadIssueCode.MALFORMED_MULTIPART,
        ),
        (
            {
                "html_file": [SimpleUploadedFile("dashboard.html", b"<html></html>")],
                "supporting_files": [SimpleUploadedFile("data.csv", b"a,b\n1,2\n")],
                "csv_files": [SimpleUploadedFile("legacy.csv", b"a,b\n3,4\n")],
            },
            UploadIssueCode.MALFORMED_MULTIPART,
        ),
        (
            {
                "package_files": [SimpleUploadedFile("dashboard.html", b"<html></html>")],
                "html_file": [SimpleUploadedFile("legacy.html", b"<html></html>")],
            },
            UploadIssueCode.MALFORMED_MULTIPART,
        ),
    ],
)
def test_upload_request_rejects_hidden_or_ambiguous_multipart_files(
    files: dict[str, list[SimpleUploadedFile]], expected_code: UploadIssueCode
) -> None:
    with pytest.raises(UploadRejected) as rejected:
        _request_upload_parts(_multipart_request(files))

    assert rejected.value.issue.code == expected_code


def test_upload_request_keeps_every_supporting_file_and_accepts_legacy_csv_key() -> None:
    request = _multipart_request(
        {
            "html_file": [SimpleUploadedFile("dashboard.html", b"<html></html>")],
            "csv_files": [
                SimpleUploadedFile("one.csv", b"a,b\n1,2\n"),
                SimpleUploadedFile("two.csv", b"a,b\n3,4\n"),
            ],
        }
    )

    assert [part.filename for part in _request_upload_parts(request)] == [
        "dashboard.html",
        "one.csv",
        "two.csv",
    ]


def test_upload_error_message_explains_the_safe_external_dependency_category() -> None:
    error = UploadRejected(
        UploadIssue(
            UploadIssueCode.EXTERNAL_DEPENDENCY,
            part_index=0,
            field=UploadIssueField.HTML_SCRIPT_SOURCE,
        )
    )

    message = _upload_error_message(error)

    assert message.startswith("Problem with a selected file:")
    assert "uses a script src" in message
    assert "JavaScript must be inline" in message


def test_upload_request_keeps_the_unified_queue_order() -> None:
    request = _multipart_request(
        {
            "package_files": [
                SimpleUploadedFile("theme.css", b"body{}"),
                SimpleUploadedFile("dashboard.html", b"<html></html>"),
                SimpleUploadedFile("data.csv", b"a,b\n1,2\n"),
            ]
        }
    )

    assert [part.filename for part in _request_upload_parts(request)] == [
        "theme.css",
        "dashboard.html",
        "data.csv",
    ]


@pytest.mark.smoke
def test_login_page_uses_one_compact_sign_in_surface(client: Client) -> None:
    response = client.get("/login/", HTTP_HOST="localhost")
    document = response.content.decode()
    parser = DocumentParser()
    parser.feed(document)

    assert response.status_code == 200
    assert '<body class="portal-page portal-page--login">' in document
    assert 'class="portal-login-card"' in document
    assert 'class="portal-form-stack portal-login-form"' in document
    assert sum(link.get("href") == "/login/" for link in parser.attributes_for("a")) == 0
    assert parser.attributes_for("button") == [
        {"class": "portal-button portal-button--primary", "type": "submit"}
    ]
    assert (
        '<button class="portal-button portal-button--primary" type="submit">Sign In</button>'
        in document
    )
    assert len(parser.attributes_for("h1")) == 1
    soeid_input = next(
        item for item in parser.attributes_for("input") if item.get("id") == "id_soeid"
    )
    assert soeid_input.get("autofocus") is None
    assert "autofocus" in soeid_input


@pytest.mark.smoke
def test_portal_shell_is_csp_compatible_and_uses_the_committed_stylesheet(client: Client) -> None:
    response = client.get("/", HTTP_HOST="localhost")
    document = response.content.decode()
    parser = DocumentParser()
    parser.feed(document)

    stylesheets = [
        link for link in parser.attributes_for("link") if link.get("rel") == "stylesheet"
    ]
    assert stylesheets == [{"rel": "stylesheet", "href": "/static/portal/foundation.css"}]
    icon_links = [
        link
        for link in parser.attributes_for("link")
        if link.get("rel") in {"icon", "apple-touch-icon"}
    ]
    assert icon_links == [
        {
            "rel": "icon",
            "type": "image/png",
            "sizes": "32x32",
            "href": "/static/portal/brand/favicon-32.png",
        },
        {
            "rel": "icon",
            "type": "image/png",
            "sizes": "192x192",
            "href": "/static/portal/brand/favicon-192.png",
        },
        {
            "rel": "apple-touch-icon",
            "sizes": "180x180",
            "href": "/static/portal/brand/apple-touch-icon.png",
        },
    ]
    assert parser.attributes_for("script") == []
    assert parser.attributes_for("style") == []
    assert all(not name.lower().startswith("on") for _, attrs in parser.elements for name in attrs)
    assert finders.find("portal/foundation.css") == str(PORTAL_CSS)
    assert finders.find("portal/brand/agora-wordmark-color.png") == str(
        BRAND_ROOT / "agora-wordmark-color.png"
    )
    assert finders.find("portal/brand/favicon-32.png") == str(BRAND_ROOT / "favicon-32.png")
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "style-src 'self'" in response.headers["Content-Security-Policy"]


def test_foundation_css_declares_responsive_accessibility_and_component_contracts() -> None:
    css = PORTAL_CSS.read_text(encoding="utf-8")

    for token in (
        "--portal-color-text",
        "--portal-color-surface",
        "--portal-color-accent",
        "--portal-space-4",
        "--portal-radius-md",
        "--portal-reading-width",
    ):
        assert token in css
    for unnamespaced_token in ("--color-text:", "--space-4:", "--font-sans:"):
        assert unnamespaced_token not in css
    for rule in (
        ".portal-card",
        ".portal-brand__wordmark",
        ".portal-table",
        ".portal-form-field",
        ".portal-button",
        ".portal-badge",
        ".portal-alert",
        ".portal-state",
        ".portal-danger-zone",
        ".portal-nav__mobile",
        ".portal-page--render",
        ".portal-render-details__panel",
        ".portal-render-details__meta",
        ".portal-public-hero",
        ".portal-public-safeguards",
        ".portal-public-safeguards__list",
        ".portal-page--login",
        ".portal-login-card",
        ".portal-page--workspace",
        ".portal-section--compact",
        ".portal-pagination__link--next",
        "overflow-x: auto",
        "overscroll-behavior-inline: contain",
        "clip-path: inset(50%)",
        "height: 100dvh",
        ":focus-visible",
        "@media (prefers-reduced-motion: reduce)",
        "@media (prefers-contrast: more)",
        "@media (forced-colors: active)",
    ):
        assert rule in css
    assert ".portal-render-shell__back" not in css
    assert ".portal-brand__mark" not in css


def test_cursor_pagination_uses_native_links_without_total_count_assumptions() -> None:
    rendered = render_component(
        "cursor-pagination",
        {
            "page": SimpleNamespace(
                previous_url="/?cursor=signed-previous",
                next_url="/?scope=shared&cursor=signed-next",
            ),
            "pagination_label": "Project results",
            "item_label": "projects",
        },
    )
    parser = DocumentParser()
    parser.feed(rendered)

    assert parser.attributes_for("nav") == [
        {"class": "portal-pagination", "aria-label": "Project results"}
    ]
    assert parser.attributes_for("a") == [
        {
            "class": "portal-pagination__link portal-pagination__link--previous",
            "href": "/?cursor=signed-previous",
            "rel": "prev",
        },
        {
            "class": "portal-pagination__link portal-pagination__link--next",
            "href": "/?scope=shared&cursor=signed-next",
            "rel": "next",
        },
    ]
    normalized_text = " ".join(parser.text.split())
    assert "Previous projects" in normalized_text
    assert "Next projects" in normalized_text
    assert "Page" not in normalized_text
    assert "total" not in normalized_text.lower()
    assert "scope=shared&amp;cursor=signed-next" in rendered


def test_cursor_pagination_is_omitted_when_there_is_no_navigation() -> None:
    rendered = render_component(
        "cursor-pagination",
        {"page": SimpleNamespace(previous_url=None, next_url=None)},
    )

    assert rendered.strip() == ""


def test_empty_cursor_page_keeps_safe_back_navigation() -> None:
    rendered = render_to_string(
        "portal/projects/list.html",
        {
            "active_scope": "mine",
            "projects": (),
            "project_page": SimpleNamespace(
                previous_url="/?cursor=signed-previous",
                next_url=None,
            ),
            "mine_url": "/",
            "shared_url": "/?scope=shared",
        },
    )
    parser = DocumentParser()
    parser.feed(rendered)

    assert "No projects in these results" in parser.text
    assert "The project list changed." in parser.text
    assert any(
        attributes.get("href") == "/?cursor=signed-previous"
        for attributes in parser.attributes_for("a")
    )
    assert "Previous projects" in " ".join(parser.text.split())


def test_empty_revision_and_access_pages_keep_safe_back_navigation() -> None:
    project_id = UUID("00000000-0000-0000-0000-000000000123")
    project = SimpleNamespace(
        id=project_id,
        name="Bounded project",
        description="",
        latest_revision=None,
        latest_revision_id=None,
        published_revision=None,
        published_revision_id=None,
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
        get_state_display=lambda: "Draft",
    )
    revision_url = f"/projects/{project_id}/?cursor=signed-previous"
    detail = render_to_string(
        "portal/projects/detail.html",
        {
            "project": project,
            "is_owner": True,
            "revisions": (),
            "revision_page": SimpleNamespace(previous_url=revision_url, next_url=None),
        },
    )
    assert "No revisions in these results" in detail
    assert revision_url in detail

    active_url = f"/projects/{project_id}/access/?active_cursor=signed-previous"
    history_url = f"/projects/{project_id}/access/?history_cursor=signed-previous"
    access = render_to_string(
        "portal/projects/access.html",
        {
            "project": project,
            "owner_soeid": "PROJECT.OWNER",
            "form": GrantViewerForm(),
            "grant_url": f"/projects/{project_id}/access/",
            "active_grants": (),
            "active_grants_page": SimpleNamespace(
                previous_url=active_url,
                next_url=None,
            ),
            "grant_history": (),
            "grant_history_page": SimpleNamespace(
                previous_url=history_url,
                next_url=None,
            ),
        },
    )
    assert "No Viewer grants in these results" in access
    assert "No revoked grants in these results" in access
    assert active_url in access
    assert history_url in access


def test_bounded_list_templates_do_not_reconstruct_cursors_or_page_totals() -> None:
    templates = (
        TEMPLATE_ROOT / "admin" / "user_list.html",
        TEMPLATE_ROOT / "projects" / "list.html",
        TEMPLATE_ROOT / "projects" / "detail.html",
        TEMPLATE_ROOT / "projects" / "access.html",
    )
    forbidden_fragments = (
        ".paginator",
        ".num_pages",
        ".previous_page_number",
        ".next_page_number",
        ".has_other_pages",
        "?page=",
        "active_cursor",
        "history_cursor",
        "<iframe",
        "<script",
        "srcdoc",
        "|safe",
    )

    for template in templates:
        source = template.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in source, f"{template.name} contains {fragment}"

    project_list = templates[1].read_text(encoding="utf-8")
    project_detail = templates[2].read_text(encoding="utf-8")
    project_access = templates[3].read_text(encoding="utf-8")
    assert "mine_count" not in project_list
    assert "shared_count" not in project_list
    assert "viewer_count" not in project_detail
    assert "viewer_count" not in project_access
    assert 'method="post" action="{{ grant_url }}"' in project_access


def test_user_search_is_a_narrow_native_get_form() -> None:
    source = (TEMPLATE_ROOT / "admin" / "user_list.html").read_text(encoding="utf-8")

    assert 'role="search" method="get" action="{{ search_url }}"' in source
    assert 'for="user-search"' in source
    assert 'id="user-search"' in source
    assert 'name="query"' in source
    assert 'type="search"' in source
    assert "search_form.query.value|default_if_none:''" in source
    assert 'aria-describedby="user-search-help{% if search_form.query.errors %} ' in source
    assert 'aria-invalid="true"' in source
    assert 'id="user-search-error" role="alert"' in source
    assert 'href="{{ clear_search_url }}"' in source
    assert "Enter the start of a SOEID." in source
    assert "Fix the search above." in source
    assert "|safe" not in source


def test_invalid_user_search_is_escaped_and_announced_without_broad_results() -> None:
    search_form = UserSearchForm({"query": "<script>"})
    assert search_form.is_valid() is False

    rendered = render_to_string(
        "portal/admin/user_list.html",
        {
            "users": (),
            "user_page": SimpleNamespace(previous_url=None, next_url=None),
            "search_form": search_form,
            "search_query": "",
            "search_url": "/admin/users/",
            "clear_search_url": "/admin/users/",
        },
    )
    parser = DocumentParser()
    parser.feed(rendered)
    search_input = next(
        attributes
        for attributes in parser.attributes_for("input")
        if attributes.get("id") == "user-search"
    )

    assert search_input.get("value") == "<script>"
    assert search_input.get("aria-invalid") == "true"
    assert search_input.get("aria-describedby") == "user-search-help user-search-error"
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert any(
        attributes.get("id") == "user-search-error" and attributes.get("role") == "alert"
        for attributes in parser.attributes_for("div")
    )
    assert "Fix the search above." in parser.text
    assert "No users have been provisioned." not in parser.text


def test_wide_data_tables_are_named_keyboard_scroll_regions() -> None:
    templates = (
        TEMPLATE_ROOT / "admin" / "user_list.html",
        TEMPLATE_ROOT / "projects" / "list.html",
        TEMPLATE_ROOT / "projects" / "detail.html",
        TEMPLATE_ROOT / "projects" / "access.html",
    )

    for template in templates:
        source = template.read_text(encoding="utf-8")
        wrapper_count = source.count('class="portal-table-wrap"')
        assert wrapper_count > 0
        assert source.count('class="portal-table-wrap" role="region"') == wrapper_count
        assert source.count('tabindex="0"') >= wrapper_count
        for labelled_by in re.findall(r'aria-labelledby="([^"]+-caption)"', source):
            assert f'id="{labelled_by}"' in source


def test_reusable_components_render_safe_semantic_markup() -> None:
    unsafe_text = "<script>never render this</script>"
    rendered = "".join(
        (
            render_component("card", {"title": "Card", "text": unsafe_text, "items": ["One"]}),
            render_component(
                "alert", {"tone": "error", "heading": "Error", "message": unsafe_text}
            ),
            render_component("empty-state", {"title": "Empty", "message": "No rows"}),
            render_component("error-state", {"title": "Failure", "message": "Try again"}),
            render_component("loading-state", {"message": "Loading rows"}),
            render_component(
                "data-table",
                {"caption": "Sample table", "headers": ["Name"], "rows": [["One"]]},
            ),
            render_component(
                "form-field",
                {
                    "field_id": "name",
                    "field_name": "name",
                    "label": "Name",
                    "help_text": "Required",
                    "error": "Invalid",
                    "required": True,
                    "value": unsafe_text,
                },
            ),
            render_component(
                "destructive-action", {"title": "Delete", "message": "This is permanent"}
            ),
        )
    )
    parser = DocumentParser()
    parser.feed(rendered)

    assert "&lt;script&gt;never render this&lt;/script&gt;" in rendered
    assert parser.attributes_for("table")[0] == {"class": "portal-table"}
    assert parser.attributes_for("caption")[0] == {}
    assert "Sample table" in rendered
    assert parser.attributes_for("th")[0].get("scope") == "col"
    assert parser.attributes_for("input")[0].get("aria-invalid") == "true"
    assert parser.attributes_for("input")[0].get("aria-describedby") == "name-help name-error"
    assert parser.attributes_for("label")[0].get("for") == "name"
    assert parser.attributes_for("input")[0].get("required") is None
    assert "required" in rendered
    assert 'role="alert"' in rendered
    assert 'role="status"' in rendered
    assert 'name="confirmation"' not in rendered


def test_reusable_state_components_do_not_create_default_id_collisions() -> None:
    rendered = "".join(
        (
            render_component("empty-state", {"message": "No rows"}),
            render_component("empty-state", {"message": "No other rows"}),
            render_component("error-state", {"message": "Try again"}),
            render_component("error-state", {"message": "Try once more"}),
            render_component("destructive-action", {"title": "Delete", "message": "Permanent"}),
            render_component("destructive-action", {"title": "Remove", "message": "Permanent"}),
        )
    )
    parser = DocumentParser()
    parser.feed(rendered)

    ids = [attrs["id"] for _, attrs in parser.elements if attrs.get("id")]
    assert ids == []


def test_card_heading_level_can_follow_the_surrounding_document() -> None:
    rendered = render_component("card", {"title": "Nested card", "heading_level": 3})
    parser = DocumentParser()
    parser.feed(rendered)

    assert len(parser.attributes_for("h3")) == 1
    assert parser.attributes_for("h2") == []


def test_empty_state_heading_level_can_follow_the_surrounding_document() -> None:
    rendered = render_component(
        "empty-state",
        {"title": "Nested state", "message": "No rows", "heading_level": 4},
    )
    parser = DocumentParser()
    parser.feed(rendered)

    assert len(parser.attributes_for("h4")) == 1
    assert parser.attributes_for("h2") == []


def test_callers_can_supply_ids_for_heading_and_form_relationships() -> None:
    rendered = "".join(
        (
            render_component(
                "empty-state",
                {"heading_id": "empty-heading", "title": "Empty", "message": "No rows"},
            ),
            render_component(
                "form-field",
                {
                    "field_id": "email",
                    "field_name": "email",
                    "label": "Email",
                    "help_text": "Use your work address.",
                },
            ),
        )
    )
    parser = DocumentParser()
    parser.feed(rendered)

    empty_section = parser.attributes_for("section")[0]
    empty_heading = parser.attributes_for("h2")[0]
    assert empty_section.get("aria-labelledby") == "empty-heading"
    assert empty_heading.get("id") == "empty-heading"
    assert parser.attributes_for("label")[0].get("for") == "email"
    assert parser.attributes_for("input")[0].get("id") == "email"
    assert parser.attributes_for("input")[0].get("aria-describedby") == "email-help"
