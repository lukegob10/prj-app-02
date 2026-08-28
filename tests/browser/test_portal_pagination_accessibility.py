from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from playwright.sync_api import Page

from agora.portal.forms import UserSearchForm

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


def test_narrow_bounded_results_keep_native_keyboard_navigation(page: Page) -> None:
    search_form = UserSearchForm({"query": "USER"})
    assert search_form.is_valid() is True
    document = render_to_string(
        "portal/admin/user_list.html",
        {
            "users": (),
            "user_page": SimpleNamespace(previous_url="#previous", next_url="#next"),
            "search_form": search_form,
            "search_query": "USER",
            "search_url": "/admin/users/",
            "clear_search_url": "/admin/users/",
        },
    )
    page.set_viewport_size({"width": 320, "height": 640})
    page.set_content(document)
    page.add_style_tag(path=str(PORTAL_CSS))

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
    table_region = page.locator(".portal-table-wrap")
    overflow = table_region.evaluate(
        "element => ({ client: element.clientWidth, scroll: element.scrollWidth })"
    )
    assert overflow["scroll"] > overflow["client"]

    for _ in range(10):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.id") == "user-search":
            break
    else:
        pytest.fail("Search field was not reachable in the first ten tab stops")

    focus_sequence = (
        "#user-search",
        '.portal-search button[type="submit"]',
        ".portal-search a",
        ".portal-table-wrap",
        ".portal-pagination__link--previous",
        ".portal-pagination__link--next",
    )
    for index, selector in enumerate(focus_sequence):
        if index:
            page.keyboard.press("Tab")
        focused = page.locator(":focus")
        assert focused.evaluate("(element, target) => element.matches(target)", selector) is True
        assert focused.evaluate("element => getComputedStyle(element).outlineWidth") == "3px"

    for selector in (
        ".portal-pagination__link--previous",
        ".portal-pagination__link--next",
    ):
        focused = page.locator(selector)
        box = focused.bounding_box()
        assert box is not None
        assert box["height"] >= 44
