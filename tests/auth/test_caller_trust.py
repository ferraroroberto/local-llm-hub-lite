"""Which callers the bearer-token gate admits without a token.

Loopback trust is for programs running on this machine. These tests pin
the edges of it on both apps the gate guards: the ``/admin`` sub-app and
the parent hub.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app_web import server as admin_server
from src import server as server_mod
from src import webapp_config as webapp_config_mod
from src.webapp_config import WebappConfig

_REMOTE = {"X-Forwarded-For": "203.0.113.5"}
_UNLISTED_ORIGIN = "https://pages.example"
_LISTED_ORIGIN = "https://llm.example.com"


def _admin(token: str = "", origins: list | None = None) -> TestClient:
    app = admin_server.create_app()
    app.state.webapp_config = WebappConfig(
        auth_token=token, cors_allow_origins=list(origins or [])
    )
    return TestClient(app)


def _parent(monkeypatch, token: str = "") -> TestClient:
    monkeypatch.setattr(
        webapp_config_mod,
        "load_webapp_config",
        lambda *a, **k: WebappConfig(auth_token=token),
    )
    return TestClient(server_mod.app)


def test_admin_without_a_token_refuses_remote_callers():
    r = _admin().get("/api/hub/status", headers=_REMOTE)
    assert r.status_code == 401


def test_parent_without_a_token_refuses_remote_callers(monkeypatch):
    r = _parent(monkeypatch).get("/v1/models", headers=_REMOTE)
    assert r.status_code == 401


def test_without_a_token_local_programs_still_pass(monkeypatch):
    assert _admin().get("/api/hub/status").status_code == 200
    assert _parent(monkeypatch).get("/v1/models").status_code == 200


def test_loopback_request_from_an_unlisted_origin_must_authenticate():
    client = _admin(token="secret123")
    r = client.get("/api/hub/status", headers={"Origin": _UNLISTED_ORIGIN})
    assert r.status_code == 401
    r = client.get(
        "/api/hub/status",
        headers={"Origin": _UNLISTED_ORIGIN, "Authorization": "Bearer secret123"},
    )
    assert r.status_code == 200


def test_loopback_request_from_an_unlisted_origin_without_a_token_is_refused():
    r = _admin().get("/api/hub/status", headers={"Origin": _UNLISTED_ORIGIN})
    assert r.status_code == 401


def test_parent_loopback_request_from_an_unlisted_origin_must_authenticate(monkeypatch):
    client = _parent(monkeypatch, token="parentsecret")
    r = client.get("/v1/models", headers={"Origin": _UNLISTED_ORIGIN})
    assert r.status_code == 401


def test_opaque_origin_is_not_local():
    r = _admin(token="secret123").get("/api/hub/status", headers={"Origin": "null"})
    assert r.status_code == 401


def test_loopback_and_listed_origins_pass():
    client = _admin(token="secret123", origins=[_LISTED_ORIGIN])
    for origin in (
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://[::1]:8000",
        _LISTED_ORIGIN,
    ):
        r = client.get("/api/hub/status", headers={"Origin": origin})
        assert r.status_code == 200, origin


def test_loopback_request_addressed_to_another_name_must_authenticate():
    client = _admin(token="secret123")
    r = client.get("/api/hub/status", headers={"Host": "hub.pages.example:8000"})
    assert r.status_code == 401
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "127.0.0.2"):
        r = client.get("/api/hub/status", headers={"Host": host})
        assert r.status_code == 200, host
