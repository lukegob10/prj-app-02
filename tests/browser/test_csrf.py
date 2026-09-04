from __future__ import annotations

import pytest
from django.test import override_settings
from playwright.sync_api import Page, expect

from .server import PORTAL_HOST, BrowserFixtureStack

pytestmark = [pytest.mark.browser, pytest.mark.only_browser("chromium")]


@override_settings(ALLOWED_HOSTS=[PORTAL_HOST], CSRF_COOKIE_SECURE=False)
def test_native_portal_form_preserves_origin_and_passes_csrf(
    page: Page,
    browser_stack: BrowserFixtureStack,
) -> None:
    form_url = browser_stack.portal.url("/fixture/csrf")
    response = page.goto(form_url)
    assert response is not None
    assert response.status == 200

    with page.expect_response(
        lambda response: response.url == form_url and response.request.method == "POST"
    ) as submission:
        page.get_by_role("button", name="Submit", exact=True).click()

    assert submission.value.status == 200
    assert submission.value.request.headers["origin"] == browser_stack.portal.origin
    expect(page.get_by_role("heading", name="Form accepted")).to_be_visible()
