"""GET /api/models — the admin Models tab's per-tile payload (lite fork).

Single-host edition: every enabled row is local, there is no placement /
fleet block, and the payload carries each row's declared startup policy
(#422) plus ownership/reachability computed from one netstat snapshot.
"""

from __future__ import annotations

import os

os.environ.setdefault("LOCAL_LLM_HUB_HOST", "local")

from fastapi.testclient import TestClient  # noqa: E402

from app_web.routers import models as models_router  # noqa: E402
from src import backend_process as bp  # noqa: E402


def _admin_client() -> TestClient:
    from app_web.server import create_app

    return TestClient(create_app())


def _row(body: dict, model_id: str) -> dict:
    rows = [m for m in body["models"] if m["id"] == model_id]
    assert rows, f"no row for {model_id!r} in {[m['id'] for m in body['models']]}"
    return rows[0]


def test_rows_carry_tile_fields_when_nothing_listens(monkeypatch):
    monkeypatch.setattr(models_router, "snapshot_listening_pids", lambda: {})

    resp = _admin_client().get("/api/models")
    assert resp.status_code == 200
    body = resp.json()

    qwen = _row(body, "qwen35_4b")
    assert qwen["display_name"] == "qwen3.5-4b"
    assert qwen["backend"] == "openai"
    assert qwen["controllable"] is True
    assert qwen["reachable"] is False        # port not bound → not probed
    assert qwen["ownership"] == "none"
    assert qwen["pid"] is None
    assert "agentic_light" in qwen["aliases"]
    # Startup-policy fields (#422) ride on every local row.
    assert qwen["startup"] == "eager"
    assert qwen["idle_unload_minutes"] is None
    assert isinstance(qwen["est_vram_mb"], int)

    whisper = _row(body, "whisper")
    assert whisper["backend"] == "whisper"
    assert whisper["controllable"] is True

    # Lite payload: no placement/fleet/config blocks anywhere.
    assert "config" not in body
    assert "host_budgets" not in body
    for row in body["models"]:
        assert "placement" not in row


def test_unbound_port_skips_http_probe(monkeypatch):
    """The netstat snapshot is a cheap reachability gate — no port bound
    means no HTTP probe is ever fired."""
    monkeypatch.setattr(models_router, "snapshot_listening_pids", lambda: {})
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: (_ for _ in ()).throw(
        AssertionError("is_reachable must not be called for an unbound port")
    ))

    body = _admin_client().get("/api/models").json()
    assert all(row["reachable"] is False for row in body["models"])


def test_bound_port_probes_and_reports_reachable(monkeypatch):
    qwen_port = 8081
    whisper_port = 8090
    monkeypatch.setattr(
        models_router, "snapshot_listening_pids",
        lambda: {qwen_port: [111], whisper_port: [222]},
    )
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: True)
    monkeypatch.setattr(bp, "is_running", lambda mid: False)

    body = _admin_client().get("/api/models").json()
    qwen = _row(body, "qwen35_4b")
    assert qwen["reachable"] is True
    # The hub didn't spawn it, but something owns the port.
    assert qwen["ownership"] == "external"
    assert qwen["pid"] == 111
    # Non-TTS rows never carry a device key.
    assert "device" not in qwen


def test_hub_owned_backend_reports_ours(monkeypatch):
    monkeypatch.setattr(models_router, "snapshot_listening_pids", lambda: {8081: [111]})
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: True)
    monkeypatch.setattr(bp, "is_running", lambda mid: mid == "qwen35_4b")
    monkeypatch.setattr(bp, "pid", lambda mid: 4242)

    body = _admin_client().get("/api/models").json()
    qwen = _row(body, "qwen35_4b")
    assert qwen["ownership"] == "ours"
    assert qwen["pid"] == 4242


def test_start_unknown_model_404():
    resp = _admin_client().post("/api/models/does-not-exist/start")
    assert resp.status_code == 404


def test_log_unknown_model_404():
    resp = _admin_client().get("/api/models/does-not-exist/log")
    assert resp.status_code == 404
