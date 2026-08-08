"""End-to-end tests for the "Model decisions" (roles) card (issue #373).

Route-intercepts the backing endpoint so the render is deterministic. The
API contract itself has its own unit tests in ``tests/test_roles_router.py``.

What this locks in:
  * the card is folded by default and fetches nothing until expanded (no
    background poll — mirrors the System Map lazy-load pattern);
  * expanding it renders one row per configured role, with the fallback
    chain visible on roles that declare one.
"""

from __future__ import annotations

import json

FAKE_ROLES = {
    "roles": {
        "agentic_light": {
            "model_id": "qwen35_4b", "display_name": "Qwen3.5 4B",
            "notes": "fast lane", "fallback": [],
        },
        "audio.transcribe": {
            "model_id": "whisper", "display_name": "Whisper Turbo",
            "notes": None, "fallback": ["whisper_backup"],
        },
    },
}


def _install_routes(page):
    def roles_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(FAKE_ROLES))

    page.route("**/admin/api/roles", roles_handler)


def _open_models_tab(page, admin_url):
    # wait_until="load" so wireTabs() has attached before the click (issue #19).
    page.goto(admin_url, wait_until="load")
    page.click("#tabModels")
    page.wait_for_selector("#paneModels", state="visible", timeout=5000)


def test_roles_card_folded_by_default_no_fetch(page, admin_url):
    """Collapsed on load; the backing endpoint is not called until expanded."""
    calls = []

    def roles_handler(route):
        calls.append(1)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(FAKE_ROLES))

    page.route("**/admin/api/roles", roles_handler)

    _open_models_tab(page, admin_url)
    page.wait_for_selector("#rolesCard", state="attached", timeout=5000)
    assert page.eval_on_selector("#rolesCard", "el => el.open") is False
    page.wait_for_timeout(300)  # give any accidental eager fetch a chance to fire
    assert calls == [], "the roles card must not fetch while collapsed"


def test_roles_card_renders_on_expand(page, admin_url):
    _install_routes(page)
    _open_models_tab(page, admin_url)
    page.eval_on_selector("#rolesCard", "el => { el.open = true; }")

    page.wait_for_selector("#rolesList .startup-row", state="visible", timeout=10000)
    roles_rows = page.locator("#rolesList .startup-row")
    assert roles_rows.count() == 2
    assert "Qwen3.5 4B" in roles_rows.nth(0).inner_text()
    # dotted audio.transcribe role renders with its fallback chain visible
    assert "fallback: whisper_backup" in roles_rows.nth(1).inner_text().lower()
