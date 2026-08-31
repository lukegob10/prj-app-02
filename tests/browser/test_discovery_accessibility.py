from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.http import HttpRequest
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import RequestFactory
from playwright.sync_api import Page

from agora.persistence.models import User
from agora.portal.discovery_forms import DashboardSearchForm, DashboardTagsForm

pytestmark = [pytest.mark.browser, pytest.mark.only_browser("chromium")]

PORTAL_CSS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agora"
    / "portal"
    / "static"
    / "portal"
    / "foundation.css"
)


def _authenticated_request(path: str = "/") -> HttpRequest:
    request = RequestFactory().get(path)
    request.user = User(soeid="VIEWER.ONE")
    get_token(request)
    return request


def _published_project(
    number: int,
    *,
    favorite: bool = False,
    has_new_publication: bool = False,
) -> SimpleNamespace:
    revision = SimpleNamespace(id=UUID(int=number + 1_000), number=number)
    tag_values = (SimpleNamespace(label="Liquidity"), SimpleNamespace(label="Daily"))
    return SimpleNamespace(
        id=UUID(int=number),
        name=f"Liquidity dashboard {number}",
        description="Authorized catalog metadata only.",
        owner=SimpleNamespace(soeid="OWNER.ONE"),
        state="published",
        published_revision=revision,
        latest_revision=revision,
        is_favorite=favorite,
        has_new_publication=has_new_publication,
        tags=SimpleNamespace(all=lambda: tag_values),
        get_state_display=lambda: "Published",
    )


def _set_portal_content(page: Page, document: str) -> None:
    page.set_viewport_size({"width": 320, "height": 720})
    page.set_content(document)
    page.add_style_tag(path=str(PORTAL_CSS))


def _assert_no_catalog_artifacts(page: Page, document: str) -> None:
    lowered = document.lower()
    assert page.locator("iframe, object, embed").count() == 0
    assert "srcdoc" not in lowered
    assert ".html" not in lowered
    assert ".csv" not in lowered
    assert "agorausercontent" not in lowered


def test_discovery_forms_normalize_plain_search_and_individual_tags() -> None:
    search_form = DashboardSearchForm({"query": "  Liquidity   \uff24aily  "})
    assert search_form.is_valid() is True
    assert search_form.cleaned_data["query"] == "Liquidity Daily"

    invalid_search = DashboardSearchForm({"query": "Risk\u200bDaily"})
    assert invalid_search.is_valid() is False
    assert invalid_search.errors["query"] == ["Search cannot contain control characters."]

    tag_form = DashboardTagsForm(
        {
            "tag_1": "  Daily   risk ",
            "tag_2": "DAILY RISK",
            "tag_3": "Treasury",
            "tag_4": "",
            "tag_5": "",
        }
    )
    assert tag_form.is_valid() is False
    assert "matches another tag after normalization" in tag_form.errors["tag_2"][0]

    valid_tags = DashboardTagsForm(
        {
            "tag_1": "  Daily   risk ",
            "tag_2": "Treasury",
            "tag_3": "",
            "tag_4": "",
            "tag_5": "",
        }
    )
    assert valid_tags.is_valid() is True
    assert valid_tags.labels == ("Daily risk", "Treasury")


def test_projects_search_is_scope_explicit_keyboard_native_and_narrow(page: Page) -> None:
    project = _published_project(1)
    search_form = DashboardSearchForm({"query": "Liquid"})
    assert search_form.is_valid() is True
    document = render_to_string(
        "portal/projects/list.html",
        {
            "active_scope": "shared",
            "projects": (project,),
            "project_page": SimpleNamespace(previous_url="#previous", next_url="#next"),
            "mine_url": "/",
            "shared_url": "/?scope=shared",
            "search_form": search_form,
            "search_query": "Liquid",
            "search_input": "Liquid",
            "search_active": True,
            "search_url": "/",
            "clear_search_url": "/?scope=shared",
        },
    )
    _set_portal_content(page, document)

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
    table_region = page.locator(".portal-table-wrap")
    overflow = table_region.evaluate(
        "element => ({ client: element.clientWidth, scroll: element.scrollWidth })"
    )
    assert overflow["scroll"] > overflow["client"]
    assert page.locator('nav[aria-label="Project scope"] [aria-current="page"]').inner_text() == (
        "Shared with me"
    )
    assert page.locator('input[name="scope"]').get_attribute("value") == "shared"
    assert page.get_by_label("Search Shared with me").get_attribute("type") == "search"
    assert (
        page.get_by_role("button", name="Add Liquidity dashboard 1 to favorites").get_attribute(
            "aria-pressed"
        )
        == "false"
    )

    page.locator("#dashboard-search").focus()
    focus_sequence = (
        '.portal-search button[type="submit"]',
        ".portal-search a",
        ".portal-table-wrap",
        ".portal-table__primary-link",
        ".portal-action-group > a",
        '.portal-action-group button[type="submit"]',
        ".portal-pagination__link--previous",
        ".portal-pagination__link--next",
    )
    for selector in focus_sequence:
        page.keyboard.press("Tab")
        focused = page.locator(":focus")
        assert focused.evaluate("(element, target) => element.matches(target)", selector) is True
        assert focused.evaluate("element => getComputedStyle(element).outlineWidth") == "3px"

    _assert_no_catalog_artifacts(page, document)


def test_projects_search_empty_and_error_states_explain_the_next_step(page: Page) -> None:
    valid_form = DashboardSearchForm({"query": "Missing"})
    assert valid_form.is_valid() is True
    empty_document = render_to_string(
        "portal/projects/list.html",
        {
            "active_scope": "mine",
            "projects": (),
            "project_page": SimpleNamespace(previous_url=None, next_url=None),
            "mine_url": "/",
            "shared_url": "/?scope=shared",
            "search_form": valid_form,
            "search_query": "Missing",
            "search_input": "Missing",
            "search_active": True,
            "search_url": "/",
            "clear_search_url": "/",
        },
    )
    _set_portal_content(page, empty_document)
    empty_state = page.get_by_role("heading", name="No dashboards match this search")
    assert empty_state.is_visible()
    empty_copy = page.locator("#project-search-empty").locator("xpath=..").inner_text()
    assert "shorter beginning" in empty_copy
    assert "Create your first project" not in page.locator("main").inner_text()

    invalid_form = DashboardSearchForm({"query": "Risk\u200bDaily"})
    assert invalid_form.is_valid() is False
    error_document = render_to_string(
        "portal/projects/list.html",
        {
            "active_scope": "shared",
            "projects": (),
            "project_page": SimpleNamespace(previous_url=None, next_url=None),
            "mine_url": "/",
            "shared_url": "/?scope=shared",
            "search_form": invalid_form,
            "search_query": "",
            "search_input": "",
            "search_active": False,
            "search_url": "/",
            "clear_search_url": "/?scope=shared",
        },
    )
    _set_portal_content(page, error_document)
    assert page.locator("#dashboard-search").get_attribute("aria-invalid") == "true"
    assert page.locator("#dashboard-search").input_value() == ""
    assert page.locator("#dashboard-search-error").get_attribute("role") == "alert"
    assert page.get_by_role("heading", name="Fix the search above").is_visible()
    assert "No shared projects" not in page.locator("main").inner_text()


def test_owner_tag_fields_have_inline_errors_and_native_keyboard_order(page: Page) -> None:
    tag_form = DashboardTagsForm(
        {
            "tag_1": "Daily risk",
            "tag_2": " daily  RISK ",
            "tag_3": "Treasury",
            "tag_4": "",
            "tag_5": "",
        }
    )
    assert tag_form.is_valid() is False
    project = _published_project(2)
    document = render_to_string(
        "portal/discovery/tags.html",
        {
            "project": project,
            "tag_form": tag_form,
            "tag_action_url": f"/projects/{project.id}/tags/",
            "cancel_url": "/",
        },
        request=_authenticated_request(f"/projects/{project.id}/tags/"),
    )
    _set_portal_content(page, document)

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
    assert page.locator('.portal-form-stack input[type="text"]').count() == 5
    assert page.get_by_label("Tag", exact=True).count() == 5
    for slot in range(1, 6):
        assert page.locator(f'input[name="tag_{slot}"]').get_attribute("id") == f"id_tag_{slot}"
    duplicate = page.locator("#id_tag_2")
    assert duplicate.get_attribute("aria-invalid") == "true"
    assert "id_tag_2-error" in (duplicate.get_attribute("aria-describedby") or "")
    assert "matches another tag after normalization" in page.locator("#id_tag_2-error").inner_text()

    page.locator("#id_tag_1").focus()
    for slot in range(2, 6):
        page.keyboard.press("Tab")
        assert page.locator(":focus").get_attribute("id") == f"id_tag_{slot}"
    page.keyboard.press("Tab")
    assert page.locator(":focus").inner_text() == "Save tags"
    assert page.locator(":focus").get_attribute("type") == "submit"
    page.keyboard.press("Tab")
    assert page.locator(":focus").inner_text() == "Cancel"
    assert page.locator(".portal-button--primary").count() == 1
    _assert_no_catalog_artifacts(page, document)
