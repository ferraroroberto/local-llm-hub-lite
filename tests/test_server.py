"""Unit tests for the server — no real backend calls.

We monkeypatch ``call_openai_chat`` (the llama-server seam) so the tests
are fast and deterministic.
"""

from __future__ import annotations

import os

os.environ.setdefault("LOCAL_LLM_HUB_HOST", "local")

from fastapi.testclient import TestClient

from src import chat_translation as chat_translation_mod
from src import server as server_mod


def _fake_openai_response(text: str = "Hello from fake backend"):
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


def test_health():
    client = TestClient(server_mod.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_messages_single_turn(monkeypatch):
    seen = {}

    def fake_call(base_url, model, messages, *, max_tokens=None,
                  temperature=None, timeout=600.0, extra=None, headers=None):
        seen["messages"] = messages
        seen["model"] = model
        return _fake_openai_response("Paris")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "system": "Answer in one word.",
            "messages": [{"role": "user", "content": "Capital of France?"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "Paris"}]
    assert body["model"] == "qwen3.5-4b"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 5
    assert body["usage"]["output_tokens"] == 7
    # System prompt becomes the first OpenAI-shape message; the user turn
    # follows verbatim.
    assert seen["messages"][0] == {"role": "system", "content": "Answer in one word."}
    assert seen["messages"][1] == {"role": "user", "content": "Capital of France?"}
    # The upstream is addressed by the row's display_name.
    assert seen["model"] == "qwen3.5-4b"


def test_messages_multi_turn_preserves_roles(monkeypatch):
    captured = {}

    def fake_call(base_url, model, messages, **kwargs):
        captured["messages"] = messages
        return _fake_openai_response("ok")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "how are you"},
            ],
        },
    )
    assert r.status_code == 200
    assert [(m["role"], m["content"]) for m in captured["messages"]] == [
        ("user", "hi"),
        ("assistant", "hello"),
        ("user", "how are you"),
    ]


def test_messages_content_blocks_flatten_to_text(monkeypatch):
    captured = {}

    def fake_call(base_url, model, messages, **kwargs):
        captured["messages"] = messages
        return _fake_openai_response("ok")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"},
                ]},
            ],
        },
    )
    assert r.status_code == 200
    assert captured["messages"][0]["content"] == "line one\nline two"


def test_messages_image_block_returns_400(monkeypatch):
    def fake_call(base_url, model, messages, **kwargs):
        raise AssertionError("upstream must not be reached for image input")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                ]},
            ],
        },
    )
    assert r.status_code == 400
    body = r.json()
    # Anthropic error envelope, not FastAPI's {"detail": ...} (#460).
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "text-only" in body["error"]["message"]


def test_messages_upstream_error_returns_502(monkeypatch):
    from src.openai_upstream import UpstreamError

    def fake_call(base_url, model, messages, **kwargs):
        raise UpstreamError("boom")

    monkeypatch.setattr(chat_translation_mod, "call_openai_chat", fake_call)

    client = TestClient(server_mod.app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "qwen3.5-4b",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 502
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    assert "boom" in body["error"]["message"]
