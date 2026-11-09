"""End-to-end checks of the vendored sign-in sheet on the /admin SPA.

The e2e hub is loopback, so it never asks for a password. These tests put a
stand-in auth layer in front of the SPA with Playwright routes (every
``/admin/api/*`` call answers 401 until it carries the good bearer token;
``/admin/api/login`` checks the password) and prove the two flows:

  * a wrong password keeps the sheet open and says so, then a correct
    password closes the sheet and stores the token under the app's
    existing ``llmhub.token`` key;
  * a device that already holds a token never sees the sheet.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.usefixtures("admin_url")

TOKEN_KEY = "llmhub.token"
GOOD_TOKEN = "e2e-good-token"
GOOD_PASSWORD = "e2e-right-password"


def _install_auth_layer(page: Page) -> None:
    def _api(route: Route) -> None:
        if route.request.url.endswith("/admin/api/login"):
            body = json.loads(route.request.post_data or "{}")
            if body.get("password") == GOOD_PASSWORD:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"token": GOOD_TOKEN}))
            else:
                route.fulfill(status=401, content_type="application/json",
                              body=json.dumps({"detail": "bad password"}))
            return
        if route.request.headers.get("authorization") == f"Bearer {GOOD_TOKEN}":
            route.continue_()
            return
        route.fulfill(status=401, content_type="application/json",
                      body=json.dumps({"detail": "auth required"}))

    page.route("**/admin/api/**", _api)


def test_wrong_password_then_correct_password_signs_in(page: Page, admin_url: str) -> None:
    _install_auth_layer(page)
    page.goto(admin_url)
    sheet = page.locator("dialog.sign-in")
    expect(sheet).to_be_visible()
    page.locator("dialog.sign-in input[type=password]").fill("nope")
    page.locator("dialog.sign-in [data-track=sign-in-submit]").click()
    expect(page.locator("dialog.sign-in [role=alert]")).to_have_text("Wrong password")
    expect(sheet).to_be_visible()
    assert page.evaluate(f"localStorage.getItem('{TOKEN_KEY}')") is None

    page.locator("dialog.sign-in input[type=password]").fill(GOOD_PASSWORD)
    page.locator("dialog.sign-in [data-track=sign-in-submit]").click()
    expect(sheet).not_to_be_visible()
    assert page.evaluate(f"localStorage.getItem('{TOKEN_KEY}')") == GOOD_TOKEN


def test_signed_in_device_never_sees_the_sheet(page: Page, admin_url: str) -> None:
    _install_auth_layer(page)
    page.add_init_script(f"localStorage.setItem('{TOKEN_KEY}', '{GOOD_TOKEN}')")
    page.goto(admin_url)
    expect(page.locator("#buildReadout")).not_to_be_empty()
    expect(page.locator("dialog.sign-in[open]")).to_have_count(0)
    assert page.evaluate(f"localStorage.getItem('{TOKEN_KEY}')") == GOOD_TOKEN
