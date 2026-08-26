from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.template import Context, Template
from django.test import Client

PORTAL_CSS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agora"
    / "portal"
    / "static"
    / "portal"
    / "foundation.css"
)


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
    response = client.get("/", HTTP_HOST="portal.agora.test")
    document = response.content.decode()
    parser = DocumentParser()
    parser.feed(document)

    assert response.status_code == 200
    assert document.lstrip().lower().startswith("<!doctype html>")
    assert parser.attributes_for("html") == [{"lang": "en"}]
    assert len(parser.attributes_for("header")) == 2
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
    assert any(link.get("class") == "portal-skip-link" for link in links)
    assert any(link.get("aria-current") == "page" for link in links)
    assert parser.attributes_for("nav")[0].get("aria-label") == "Primary navigation"
    assert parser.attributes_for("summary")[0].get("aria-controls") == "primary-navigation-menu"

    headings = [element for element, _ in parser.elements if element in {"h1", "h2", "h3"}]
    assert headings.count("h1") == 1
    assert headings[:2] == ["h1", "h2"]
    assert "Secure dashboard publishing starts here" in parser.text
    assert "Uploaded content is not enabled" in parser.text
    assert "No unavailable actions are shown" in parser.text
    assert "recommend" not in document.lower()


@pytest.mark.smoke
def test_portal_shell_is_csp_compatible_and_uses_the_committed_stylesheet(client: Client) -> None:
    response = client.get("/", HTTP_HOST="portal.agora.test")
    document = response.content.decode()
    parser = DocumentParser()
    parser.feed(document)

    stylesheets = [
        link for link in parser.attributes_for("link") if link.get("rel") == "stylesheet"
    ]
    assert stylesheets == [{"rel": "stylesheet", "href": "/static/portal/foundation.css"}]
    assert parser.attributes_for("script") == []
    assert parser.attributes_for("style") == []
    assert all(not name.lower().startswith("on") for _, attrs in parser.elements for name in attrs)
    assert finders.find("portal/foundation.css") == str(PORTAL_CSS)
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
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
        ".portal-table",
        ".portal-form-field",
        ".portal-button",
        ".portal-badge",
        ".portal-alert",
        ".portal-state",
        ".portal-danger-zone",
        ".portal-nav__mobile",
        ":focus-visible",
        "@media (prefers-reduced-motion: reduce)",
        "@media (prefers-contrast: more)",
        "@media (forced-colors: active)",
    ):
        assert rule in css


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
