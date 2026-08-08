"""Model-name → backend routing in src.server.

Exercises the /v1/messages path for an openai-backed model (dispatched to
mocked `call_openai_chat`) plus the unknown-model 400 branch and the
/v1/models listing.
"""

from __future__ import annotations

import os

os.environ.setdefault("LOCAL_LLM_HUB_HOST", "local")

from fastapi.testclient import TestClient

from src import chat_translation as chat_translation_mod
from src import server as server_mod


def _fake_openai_response(text: str = "pong"):
    return {
        "id": "chatcmpl-xyz",
        "object": "chat.completion",
        "model": "qwen3.5-4b",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def test_messages_routes_openai_backend(monkeypatch):
    captured = {}

    def fake_call(base_url, model, messages, *, max_tokens=None, temperature=None, timeout=600.0, extra=None, headers=None):
        captured["base_url"] = base_url
        captured["model"] = model
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return _fake_openai_response("pong")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "system": "Answer briefly.",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == [{"type": "text", "text": "pong"}]
    assert body["model"] == "qwen3.5-4b"
    assert body["stop_reason"] == "end_turn"

    # System prompt was prepended to the OpenAI messages.
    assert captured["model"] == "qwen3.5-4b"
    assert captured["messages"][0] == {"role": "system", "content": "Answer briefly."}
    assert captured["messages"][1]["role"] == "user"
    assert "127.0.0.1:8081" in captured["base_url"]


def test_messages_routes_role_alias(monkeypatch):
    """`agentic_light` alias resolves to the qwen row; the upstream is
    addressed by the row's display_name, not the alias."""
    captured = {}

    def fake_call(base_url, model, messages, **kwargs):
        captured["model"] = model
        return _fake_openai_response("ok")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "agentic_light",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert captured["model"] == "qwen3.5-4b"


def test_messages_unknown_model_400():
    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "does-not-exist",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 400
    # Anthropic error envelope on the Anthropic-shape route (#460).
    assert "unknown model" in r.json()["error"]["message"]


def test_messages_whisper_backend_rejected_as_non_chat():
    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "whisper-large-v3-turbo",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 400
    assert "ASR backend" in r.json()["error"]["message"]


def test_chat_completions_passthrough_openai(monkeypatch):
    def fake_call(base_url, model, messages, *, max_tokens=None, temperature=None, timeout=600.0, extra=None, headers=None):
        return _fake_openai_response("hi")

    monkeypatch.setattr(server_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen3.5-4b",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Passthrough preserves OpenAI shape.
    assert body["choices"][0]["message"]["content"] == "hi"
    assert body["object"] == "chat.completion"


def test_chat_completions_whisper_backend_rejected_as_non_chat():
    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "whisper-large-v3-turbo",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 400
    # OpenAI-shape route keeps FastAPI's {"detail": ...} envelope.
    assert "ASR backend" in r.json()["detail"]


def test_list_models_includes_enabled():
    client = TestClient(server_mod.app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = {entry["id"] for entry in r.json()["data"]}
    # The qwen row under every name a client can send.
    assert "qwen35_4b" in ids
    assert "qwen3.5-4b" in ids
    assert "agentic_light" in ids
    assert "agentic_heavy" in ids
    # The whisper row + its role alias.
    assert "whisper" in ids
    assert "whisper-large-v3-turbo" in ids
    assert "audio_transcribe" in ids
