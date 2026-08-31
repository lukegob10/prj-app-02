from __future__ import annotations

import json
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import FrameLocator, Page, expect

from agora.rendering.security import content_security_policy, portal_content_security_policy

from .server import BrowserFixtureStack

pytestmark = [pytest.mark.browser, pytest.mark.only_browser("chromium")]


def test_hostile_content_is_opaque_and_common_exfiltration_channels_are_blocked(
    page: Page, browser_stack: BrowserFixtureStack
) -> None:
    content_url = browser_stack.content.url("/fixture/hostile")
    with page.expect_response(lambda response: response.url == content_url) as content_event:
        portal_response = page.goto(browser_stack.portal.url("/fixture/hostile"))
    content_response = content_event.value

    assert portal_response is not None
    assert content_response.status == 200
    assert portal_response.headers["content-security-policy"] == portal_content_security_policy(
        browser_stack.content.origin
    )
    assert content_response.headers["content-security-policy"] == content_security_policy(
        browser_stack.portal.origin
    )
    assert content_response.headers["cache-control"] == "private, no-store"
    assert content_response.headers["x-content-type-options"] == "nosniff"
    assert content_response.headers["referrer-policy"] == "no-referrer"
    assert content_response.headers["permissions-policy"] == (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), "
        "payment=(), usb=()"
    )
    assert content_response.headers["x-dns-prefetch-control"] == "off"
    assert "x-frame-options" not in content_response.headers
    assert "access-control-allow-origin" not in content_response.headers

    assert page.locator("#hostile-content").get_attribute("sandbox") == "allow-scripts"
    assert page.locator("#hostile-content").get_attribute("referrerpolicy") == "no-referrer"
    assert page.locator("#hostile-content").get_attribute("srcdoc") is None
    iframe_url = page.locator("#hostile-content").get_attribute("src")
    assert iframe_url is not None
    assert iframe_url.startswith(browser_stack.content.origin)
    frame = _frame(page, "hostile-content")
    results = _wait_for_results(frame)

    # The sandboxed document's storage/cookie and document-domain failures are the
    # observable opaque-origin evidence; location.origin can serialize the URL origin
    # in Chromium even while the sandboxed origin flag is enforced.
    assert results["parent-dom"] == "blocked"
    assert results["document-domain"] == "blocked"
    assert results["cookie"] in {"", "blocked"}
    assert results["top-navigation"] == "blocked"
    assert results["popup"] == "blocked"
    assert results["form"] in {"attempted", "blocked"}
    assert results["worker"] == "blocked"
    assert results["service-worker"] in {"blocked", "unsupported"}
    assert page.url == browser_stack.portal.url("/fixture/hostile")
    assert page.locator("#portal-marker").text_content() == (
        "Portal DOM marker must remain unchanged."
    )

    attempted_paths = {
        "/exfil/fetch",
        "/exfil/xhr",
        "/exfil/websocket",
        "/exfil/event-source",
        "/exfil/beacon",
        "/exfil/image",
        "/exfil/font",
        "/exfil/css",
        "/exfil/media",
        "/exfil/form",
        "/exfil/popup",
        "/exfil/top-navigation",
        "/exfil/worker.js",
        "/exfil/service-worker.js",
        "/exfil/script.js",
        "/exfil/stylesheet.css",
        "/exfil/nested-frame",
        "/exfil/object",
        "/exfil/manifest.json",
        "/exfil/prefetch",
        "/exfil",
    }
    assert {
        request.path for request in browser_stack.attacker.requests()
    } & attempted_paths == set()
    assert all(
        request.cookie is None for request in browser_stack.content.requests_for("/fixture/hostile")
    )
    assert all(
        cookie["domain"] == "portal.agora.test"
        for cookie in page.context.cookies()
        if cookie["name"] == "portal_probe"
    )
    assert not any(cookie["name"] == "content_probe" for cookie in page.context.cookies())


def test_two_hostile_documents_do_not_share_durable_browser_storage(
    page: Page, browser_stack: BrowserFixtureStack
) -> None:
    page.goto(browser_stack.portal.url("/fixture/storage-a"))
    first = _wait_for_results(_frame(page, "hostile-a"))
    page.goto(browser_stack.portal.url("/fixture/storage-b"))
    second = _wait_for_results(_frame(page, "hostile-b"))

    first_storage = cast(dict[str, object], first["storage"])
    second_storage = cast(dict[str, object], second["storage"])
    assert first_storage.get("localStorage") in {"blocked", "writable"}
    assert first_storage.get("sessionStorage") in {"blocked", "writable"}
    assert second_storage.get("localStorage") in {"blocked", "isolated"}
    assert second_storage.get("sessionStorage") in {"blocked", "isolated"}
    assert first_storage.get("cacheStorage") in {"blocked", "isolated", "writable"}
    assert second_storage.get("cacheStorage") in {"blocked", "isolated"}
    assert first_storage.get("indexedDB") in {"blocked", "writable"}
    assert second_storage.get("indexedDB") in {"blocked", "isolated"}
    assert first_storage.get("worker") == "blocked"
    assert second_storage.get("worker") == "blocked"
    assert first["cookie"] in {"", "blocked"}
    assert second["cookie"] in {"", "blocked"}
    assert first["documentDomain"] == "blocked"
    assert second["documentDomain"] == "blocked"
    assert not any(
        request.path.startswith("/storage-exfil") for request in browser_stack.attacker.requests()
    )
    assert all(request.cookie is None for request in browser_stack.content.requests())


def test_opaque_sandbox_can_fetch_same_revision_csv(
    page: Page,
    browser_stack: BrowserFixtureStack,
) -> None:
    page.goto(browser_stack.portal.url("/fixture/csv"))
    frame = _frame(page, "csv-content")
    body = frame.locator("body")
    expect(body).to_have_attribute("data-ready", "true", timeout=5_000)

    assert body.get_attribute("data-csv") == "loaded"
    csv_requests = browser_stack.content.requests_for("/fixture/data.csv")
    assert len(csv_requests) == 1
    assert csv_requests[0].cookie is None


def test_opaque_sandbox_can_load_same_revision_package_assets(
    page: Page,
    browser_stack: BrowserFixtureStack,
) -> None:
    page.goto(browser_stack.portal.url("/fixture/package"))
    frame = _frame(page, "package-content")
    body = frame.locator("body")
    expect(body).to_have_attribute("data-ready", "true", timeout=5_000)

    assert body.get_attribute("data-css") == "loaded"
    assert body.get_attribute("data-image") == "loaded"
    assert len(browser_stack.content.requests_for("/fixture/package.css")) == 1
    assert len(browser_stack.content.requests_for("/fixture/package.png")) == 1
    assert all(
        request.cookie is None
        for request in (
            *browser_stack.content.requests_for("/fixture/package.css"),
            *browser_stack.content.requests_for("/fixture/package.png"),
        )
    )


def test_clickjacking_is_blocked_and_content_accepts_only_the_portal_ancestor(
    page: Page, browser_stack: BrowserFixtureStack
) -> None:
    page.goto(browser_stack.attacker.url("/frame-portal"))
    page.wait_for_load_state("networkidle")
    assert not _frame_contains_marker(page, "portal-page")

    page.goto(browser_stack.attacker.url("/frame-content"))
    page.wait_for_load_state("networkidle")
    assert not _frame_contains_marker(page, "content-title")


def _frame(page: Page, frame_id: str) -> FrameLocator:
    return page.frame_locator(f"#{frame_id}")


def _wait_for_results(frame: FrameLocator) -> dict[str, object]:
    body = frame.locator("body")
    expect(body).to_have_attribute("data-ready", "true", timeout=5_000)
    raw_results = frame.locator("[data-test='results']").get_attribute("data-results")
    assert raw_results is not None
    parsed = json.loads(raw_results)
    assert isinstance(parsed, dict)
    assert "fatal" not in parsed, parsed
    return cast(dict[str, object], parsed)


def _frame_contains_marker(page: Page, marker: str) -> bool:
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            if frame.locator(f"[data-test='{marker}']").count() > 0:
                return True
        except PlaywrightError:
            continue
    return False
