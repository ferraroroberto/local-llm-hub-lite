"""One bearer-token source for the hub and its /admin sub-app.

The token is read from ``config/webapp_config.json`` by both apps, re-read
when the file changes and not otherwise, and a missing file is logged once
instead of on every request.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi.testclient import TestClient

from app_web import server as admin_server
from src import server as server_mod
from src import webapp_config
from tests._webapp_token import pin_auth_token

_REMOTE = {"X-Forwarded-For": "203.0.113.5"}


def _bearer(token: str) -> dict:
    return {**_REMOTE, "Authorization": f"Bearer {token}"}


def _rewrite(token: str) -> None:
    """Rotate the token on disk the way an operator editing the file would."""
    path = webapp_config.DEFAULT_CONFIG_PATH
    path.write_text(json.dumps({"auth_token": token}), encoding="utf-8")
    # A same-size rewrite inside one clock tick is invisible to a stat key.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_rotation_on_disk_reaches_both_apps_without_a_restart():
    pin_auth_token("old-token-0001")
    hub = TestClient(server_mod.app)
    admin = TestClient(admin_server.create_app())
    assert hub.get("/v1/models", headers=_bearer("old-token-0001")).status_code == 200
    assert admin.get("/api/hub/status", headers=_bearer("old-token-0001")).status_code == 200

    _rewrite("new-token-0002")

    for client, path in ((hub, "/v1/models"), (admin, "/api/hub/status")):
        assert client.get(path, headers=_bearer("old-token-0001")).status_code == 401
        assert client.get(path, headers=_bearer("new-token-0002")).status_code == 200


def test_a_missing_file_is_logged_once_across_many_requests(caplog):
    caplog.set_level(logging.INFO, logger="src.webapp_config")
    hub = TestClient(server_mod.app)
    for _ in range(5):
        assert hub.get("/v1/models").status_code == 200  # loopback, no token needed
    assert caplog.text.count("webapp_config not found") == 1


def test_a_token_minted_after_start_is_picked_up():
    hub = TestClient(server_mod.app)
    assert hub.get("/v1/models", headers=_REMOTE).status_code == 401  # no token: only local callers
    pin_auth_token("minted-by-tray")
    assert hub.get("/v1/models", headers=_bearer("minted-by-tray")).status_code == 200


def test_an_unreadable_file_means_no_token_and_one_warning(caplog):
    webapp_config.DEFAULT_CONFIG_PATH.write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="src.webapp_config")
    assert [webapp_config.current_auth_token() for _ in range(3)] == ["", "", ""]
    assert caplog.text.count("Could not read") == 1
