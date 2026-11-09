"""End-to-end smoke tests on the /admin SPA.

What the gate proves:

  * Each tab pane (Hub / Models / Playground) opens from the nav on a
    phone viewport without a JavaScript console error, and is screenshotted
    into ``tests/e2e/snapshots/`` (gitignored) for visual review.
  * The Small/Default/Large text-size control scales and persists.
  * A request to a known-bad model lands once in the live-request pane over
    SSE, and is not duplicated when the stream reconnects.

The API-only checks (version endpoint, ?v=<hash> stamps, the request ring,
toast stacking) are unit tests: ``tests/test_build_info.py``,
``tests/test_static_versioning.py`` and ``tests/test_server.py``.

Runs under Chromium only. WebKit was dropped from the matrix in issue
#24 — the SPA has no Safari-specific code and WebKit on windows-latest
CI was chronically flaky.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("admin_url")

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
PHONE_VIEWPORT = {"width": 390, "height": 844}


@pytest.fixture(autouse=True)
def _no_console_errors(page):
    """Fail the test if the page logs an actual ``console.error`` line.

    Failures from missing icon-180.png are noise we don't care about
    here — the icon family swap is tracked separately (issue #6). Any
    *other* error fails fast.
    """
    errs = []

    def _on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        # Tolerated noise — placeholder PNG icons occasionally 404 while
        # the manifest is still settling on first paint.
        if "icon-180" in text or "icon-512" in text:
            return
        errs.append(text)

    page.on("console", _on_console)
    yield
    if errs:
        raise AssertionError("console errors: " + " | ".join(errs))


def _snapshot(page, name: str, browser_name: str) -> None:
    """Save a phone-viewport screenshot of the current page to a
    deterministic path under ``tests/e2e/snapshots/``. The sparklines
    region is masked because it re-renders every 2.5 s from live
    RAM/GPU readings — masking keeps the snapshot useful for human
    visual review without false diffs.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAPSHOT_DIR / f"{name}-{browser_name}.png"
    spark = page.locator("#hubSparklines")
    mask = [spark] if spark.count() > 0 else []
    page.screenshot(path=str(out), full_page=True, mask=mask)
    assert out.exists() and out.stat().st_size > 0, f"empty screenshot at {out}"


def test_each_tab_opens_on_a_phone(page, admin_url, browser_name):
    """Hub is the default pane; Models and Playground open from the nav and
    the Playground model dropdown fills. Each tab is screenshotted."""
    page.set_viewport_size(PHONE_VIEWPORT)
    # wait_until="load" — not "domcontentloaded" — so the module-script
    # boot() has finished and wireTabs() has attached its listeners before
    # we click. Otherwise the click lands on a button with no handler and
    # the pane stays hidden (issue #19).
    page.goto(admin_url, wait_until="load")
    page.wait_for_selector("#tabHub", state="visible", timeout=5000)
    page.wait_for_selector("#paneHub", state="visible", timeout=3000)
    page.wait_for_selector("#paneModels", state="hidden", timeout=3000)
    page.wait_for_selector("#panePlayground", state="hidden", timeout=3000)
    # Let the first poll settle so the snapshot reflects the populated
    # UI rather than the "checking…" placeholder in the Hub card header.
    page.wait_for_function(
        "document.getElementById('hubLiveStatusText') && "
        "document.getElementById('hubLiveStatusText').textContent.indexOf('checking') === -1",
        timeout=8000,
    )
    _snapshot(page, "hub-390x844", browser_name)

    page.click("#tabModels")
    page.wait_for_selector("#paneModels", state="visible", timeout=3000)
    _snapshot(page, "models-390x844", browser_name)

    page.click("#tabPlayground")
    page.wait_for_selector("#panePlayground", state="visible", timeout=3000)
    # Model dropdown populated from /admin/api/playground/models. Boot
    # fans out several fetches at once, including install_status which
    # is the slow one — 10s window covers the cold-cache case.
    page.wait_for_function(
        "document.getElementById('playgroundModel').options.length > 0",
        timeout=10000,
    )
    _snapshot(page, "playground-390x844", browser_name)


def test_text_size_control_persists(page, admin_url):
    """The Hub-tab Small/Default/Large control scales the root font-size,
    survives a reload via the pre-paint stamp, and works in both themes (#34)."""
    page.goto(admin_url, wait_until="load")
    for theme in ("light", "dark"):
        page.evaluate(
            "(t) => { localStorage.setItem('llmhub.theme', t); localStorage.removeItem('llmhub.textsize'); }",
            theme,
        )
        page.reload(wait_until="load")
        assert page.evaluate("document.documentElement.dataset.theme") == theme
        assert page.evaluate("document.documentElement.dataset.textsize") == "default"

        page.click("#installCard > summary")
        for step, px in (("large", "18px"), ("small", "15px"), ("default", "16px")):
            page.click(f"#textSizeControl [data-textsize='{step}']")
            assert page.evaluate("document.documentElement.dataset.textsize") == step
            assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == px
            active = page.locator("#textSizeControl .segmented-item[aria-pressed='true']")
            assert active.count() == 1 and active.get_attribute("data-textsize") == step

        page.click("#textSizeControl [data-textsize='large']")
        page.reload(wait_until="load")
        # Pre-paint stamp: set from storage before any control is bound or clicked.
        assert page.evaluate("document.documentElement.dataset.textsize") == "large"
        assert page.evaluate("getComputedStyle(document.documentElement).fontSize") == "18px"


def test_live_requests_stream_rolls_forward(page, admin_url):
    """Regression guard for the live-stream pane.

    The bug this catches (issue #10 item 2): SSE seed fills the list,
    but subsequent frames never reach the UI, so the pane freezes at
    its initial snapshot. Boot the page, take a baseline count of
    #liveRequestsList rows, fire two distinct /v1/messages from this
    test process, and assert both rows show up within a short window.
    """
    base = admin_url.rsplit("/admin/", 1)[0]
    # wait_until="load" — consistent with test_models_tab (issue #19);
    # the later #tabModels click then can't race wireTabs() either.
    page.goto(admin_url, wait_until="load")
    page.wait_for_selector("#paneHub", state="visible", timeout=5000)
    page.wait_for_selector("#liveRequestsList", state="attached", timeout=3000)
    # Let the EventSource open + the initial seed drain. We don't rely
    # on baseline counts because earlier tests in the same session may
    # have left records in the server-side ring.
    page.wait_for_timeout(700)

    # Use distinguishable model names so each marker is a unique row.
    marker_a = "e2e-rolls-forward-A"
    marker_b = "e2e-rolls-forward-B"
    for marker in (marker_a, marker_b):
        r = httpx.post(
            f"{base}/v1/messages",
            json={"model": marker, "messages": [{"role": "user", "content": "hi"}]},
            timeout=5.0,
        )
        assert r.status_code == 400, r.text

    # Both markers must arrive in the live list. 4000ms was tight enough that
    # the multi-host proxying routing added by #181 pushed CI runners (slower
    # than local) past it on every run since 2026-07-02 (tracked + resolved in
    # #192); 10000ms matches the window already used elsewhere in this file
    # for slower CI operations.
    page.wait_for_function(
        "(args) => {"
        "  const ul = document.getElementById('liveRequestsList');"
        "  if (!ul) return false;"
        "  const text = ul.textContent || '';"
        "  return text.indexOf(args.a) !== -1 && text.indexOf(args.b) !== -1;"
        "}",
        arg={"a": marker_a, "b": marker_b},
        timeout=10000,
    )

    def _marker_counts():
        return page.evaluate(
            "(markers) => {"
            "  const ul = document.getElementById('liveRequestsList');"
            "  const items = Array.from(ul ? ul.children : []);"
            "  const out = {};"
            "  for (const m of markers) {"
            "    out[m] = items.filter(li => (li.textContent || '').includes(m)).length;"
            "  }"
            "  return out;"
            "}",
            [marker_a, marker_b],
        )

    counts = _marker_counts()
    assert counts[marker_a] == 1, f"marker_a duplicated on first delivery: {counts}"
    assert counts[marker_b] == 1, f"marker_b duplicated on first delivery: {counts}"

    # Bounce the SSE: switch away from Hub and back. main.js stops the
    # stream on tab-out and starts it again on tab-in — the server then
    # replays its 20-record seed, which used to duplicate every visible
    # row in the pane.
    page.click("#tabModels")
    page.wait_for_selector("#paneModels", state="visible", timeout=2000)
    page.wait_for_timeout(300)
    page.click("#tabHub")
    page.wait_for_selector("#paneHub", state="visible", timeout=2000)
    # Give the fresh EventSource time to receive the replayed seed.
    page.wait_for_timeout(900)
    counts_after = _marker_counts()
    assert counts_after[marker_a] == 1, (
        f"marker_a duplicated after SSE reconnect: {counts_after}"
    )
    assert counts_after[marker_b] == 1, (
        f"marker_b duplicated after SSE reconnect: {counts_after}"
    )
