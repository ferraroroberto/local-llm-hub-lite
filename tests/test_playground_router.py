"""Unit tests for app_web/routers/playground.py (lite fork).

The Playground routes proxy in-process to the hub's /v1/messages and
/v1/audio/transcriptions over loopback. Here we mock the shared httpx
client seam (``get_async_client``) and assert the routes build the right
payloads. Attachments were dropped with the claude/gemini backends —
send is text-only, and the new /api/playground/transcribe proxies audio.
"""

from __future__ import annotations

import os

os.environ.setdefault("LOCAL_LLM_HUB_HOST", "local")

from fastapi.testclient import TestClient

from app_web.routers import playground as playground_router
from src import server as server_mod


class _FakeResp:
    is_success = True
    status_code = 200
    text = ""
    headers: dict = {}

    def json(self):
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


def _mock_upstream(monkeypatch, resp=None) -> dict:
    """Patch the shared httpx client so the proxied payload is captured."""
    captured: dict = {}

    class _FakeClient:
        async def post(self, url, json=None, files=None, **kw):
            captured["url"] = url
            captured["payload"] = json
            captured["files"] = files
            return resp or _FakeResp()

    monkeypatch.setattr(playground_router, "get_async_client", lambda: _FakeClient())
    return captured


def test_playground_send_is_text_only(monkeypatch):
    captured = _mock_upstream(monkeypatch)
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/send",
        data={"model": "qwen3.5-4b", "prompt": "hi", "max_tokens": "64"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "ok"
    assert body["stop_reason"] == "end_turn"
    content = captured["payload"]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "hi"}]
    # The upstream is addressed by the row's display_name over loopback.
    assert captured["payload"]["model"] == "qwen3.5-4b"
    assert captured["url"].endswith("/v1/messages")


def test_playground_send_forwards_system_prompt(monkeypatch):
    captured = _mock_upstream(monkeypatch)
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/send",
        data={"model": "qwen3.5-4b", "prompt": "hi", "max_tokens": "64",
              "system": "Be brief."},
    )
    assert r.status_code == 200, r.text
    assert captured["payload"]["system"] == "Be brief."


def test_playground_send_unknown_model_400(monkeypatch):
    _mock_upstream(monkeypatch)
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/send",
        data={"model": "does-not-exist", "prompt": "hi", "max_tokens": "64"},
    )
    assert r.status_code == 400
    assert "unknown model" in r.json()["detail"]


def test_playground_send_surfaces_upstream_anthropic_error(monkeypatch):
    """The hub answers /v1/messages errors in the Anthropic envelope (#460);
    the playground unwraps `error.message` into its own detail."""

    class _Err:
        is_success = False
        status_code = 400
        text = ""
        headers: dict = {}

        def json(self):
            return {"type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad request"}}

    _mock_upstream(monkeypatch, resp=_Err())
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/send",
        data={"model": "qwen3.5-4b", "prompt": "hi", "max_tokens": "64"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "bad request"


def test_playground_models_excludes_whisper():
    client = TestClient(server_mod.app)
    r = client.get("/admin/api/playground/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["models"]}
    assert "qwen35_4b" in ids
    assert "whisper" not in ids   # ASR-only, doesn't speak /v1/messages


# --------------------------------------------------------------------- #
# /api/playground/transcribe — the loopback ASR proxy
# --------------------------------------------------------------------- #

class _FakeASRResp:
    is_success = True
    status_code = 200
    text = ""
    headers = {"x-hub-served-model": "whisper"}

    def json(self):
        return {"text": "hello world"}


def test_playground_transcribe_proxies_and_reports_served_model(monkeypatch):
    captured = _mock_upstream(monkeypatch, resp=_FakeASRResp())
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/transcribe",
        files={"file": ("clip.wav", b"RIFFfake-wav-bytes", "audio/wav")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "hello world"
    assert body["served_model"] == "whisper"
    # Forwarded to the hub's own transcription proxy with the file intact.
    assert captured["url"].endswith("/v1/audio/transcriptions")
    name, payload, media = captured["files"]["file"]
    assert name == "clip.wav"
    assert payload == b"RIFFfake-wav-bytes"
    assert media == "audio/wav"


def test_playground_transcribe_empty_file_400(monkeypatch):
    _mock_upstream(monkeypatch, resp=_FakeASRResp())
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/transcribe",
        files={"file": ("clip.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_playground_transcribe_upstream_error_passes_status(monkeypatch):
    class _Down:
        is_success = False
        status_code = 503
        text = ""
        headers: dict = {}

        def json(self):
            return {"detail": "whisper-server not running on :8190"}

    _mock_upstream(monkeypatch, resp=_Down())
    client = TestClient(server_mod.app)
    r = client.post(
        "/admin/api/playground/transcribe",
        files={"file": ("clip.wav", b"RIFF", "audio/wav")},
    )
    assert r.status_code == 503
    assert "not running" in r.json()["detail"]
