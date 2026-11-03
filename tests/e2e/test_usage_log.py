"""The shipped usage client reaches the mounted usage router (#112)."""

from __future__ import annotations

import httpx


def test_click_is_recorded_in_usage_summary(page, admin_url, hub_url):
    """A tap on a ``data-track`` control lands in ``/admin/api/usage/summary``.

    The router is mounted under ``/admin``, so a client posting to the vendored default
    ``/api/usage/events`` would 404 and the summary would stay at 0 events.
    """
    posts = []
    page.on(
        "response",
        lambda r: posts.append(r.status) if r.url.endswith("/api/usage/events") else None,
    )
    page.goto(admin_url)
    page.locator('[data-track="theme-toggle"]').click()
    # Hiding the page flushes the client queue at once, instead of waiting out its 10 s timer.
    with page.expect_response(lambda r: r.url.endswith("/api/usage/events")):
        page.evaluate(
            "Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});"
            "document.dispatchEvent(new Event('visibilitychange'));"
        )
    assert posts and all(code == 200 for code in posts), posts
    summary = httpx.get(f"{hub_url}/admin/api/usage/summary", timeout=5.0).json()
    assert summary["events"] > 0, summary
