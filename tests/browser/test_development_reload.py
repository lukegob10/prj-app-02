from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, Route

DEVELOPMENT_RELOAD_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agora"
    / "portal"
    / "static"
    / "portal"
    / "development-reload.js"
)


def test_development_reload_never_polls_or_replays_a_post_response(page: Page) -> None:
    requests: list[str] = []

    def record_request(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(status=200, body="changed-version", content_type="text/plain")

    page.route("https://portal.agora.test/__dev__/reload/", record_request)
    source = DEVELOPMENT_RELOAD_JS.read_text(encoding="utf-8")
    page.set_content(
        "<script "
        'data-reload-request-method="POST" '
        'data-reload-url="https://portal.agora.test/__dev__/reload/" '
        'data-reload-version="original-version">'
        f"{source}</script>"
    )

    page.wait_for_timeout(900)

    assert requests == []
