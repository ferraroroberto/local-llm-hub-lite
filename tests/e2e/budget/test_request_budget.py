"""The admin page keeps its request budget on a real browser clock.

The probe (``_request_budget.py``, vendored from project-scaffolding) loads
``/admin/`` on a fake clock and counts every ``/admin/api/`` request: a minute
with the page visible, a minute hidden, and the return to the foreground. The
visible budget is ``[request_budget] visible_requests_per_minute`` in
``.fleet.toml``; a missing key raises ``RequestBudgetUnknown``, never a pass.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from tests.e2e.budget._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# The Hub tab's two push streams are one long request each, opened once at load, never a rate.
# The probe waits for every request to finish, so each is answered with one finished event
# stream; `retry` pushes the browser's reconnect past the test so none counts as a poll.
_STREAMS = ("**/admin/api/hub/requests/stream", "**/admin/api/hub/log/tail")
_NO_RECONNECT = "retry: 3600000\n\n"

# The one request a hidden page may make: the usage log sends the clicks and views it has
# queued the moment the tab is hidden, because a hidden tab may be discarded before its
# next timer. It is one batched send, not a poll, and it sends only a non-empty queue.
_HIDDEN_ALLOWED = {"/admin/api/usage/events": 1}


def _without_allowed_hidden(hidden: Counter[str]) -> Counter[str]:
    """`hidden` minus the named exceptions, so anything beyond them still breaks the budget."""
    return Counter({path: n - _HIDDEN_ALLOWED.get(path, 0) for path, n in hidden.items() if n > _HIDDEN_ALLOWED.get(path, 0)})


def test_admin_page_keeps_its_request_budget(page, admin_url):
    for pattern in _STREAMS:
        page.route(pattern, lambda route: route.fulfill(status=200, content_type="text/event-stream", body=_NO_RECONNECT))
    reading = measure_request_budget(page, admin_url, api_prefix="/admin/api/")
    reading.hidden = _without_allowed_hidden(reading.hidden)
    assert_request_budget(reading, visible_budget(REPO_ROOT))
