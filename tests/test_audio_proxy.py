"""Single-backend audio proxy contract (lite fork).

The lite hub forwards ``/v1/audio/transcriptions`` to the one local
whisper-server. No role chains, no failover — but the strict
explicit-model contract (#412) survives: an explicit *known* model id is
never answered by a different model, rejections are as observable as
successes, and the requested/served header trio is echoed on every
response.

The whisper worker binaries are platform/GPU-specific, so these fake the
httpx client and the config rather than driving a real backend.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from fastapi import HTTPException

from src import model_registry, server_audio_asr
from src.hub_observability import ObservabilityCtx


# --------------------------------------------------------------------------- #
# config helpers (temp models.yaml via conftest's `write_config` fixture)
# --------------------------------------------------------------------------- #
def _single_whisper_config(write_config, monkeypatch):
    """``wa`` is the one locally-served whisper row; ``wdown`` is a configured
    whisper row this host is *not* enabled for; ``chatty`` is a non-audio
    row."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {"pc": {"platform": "win32", "default": True, "enabled": ["wa"]}},
        "models": {
            "wa": {"display_name": "whisper-a", "backend": "whisper",
                   "engine": "whisper-server", "port": 9001},
            "wdown": {"display_name": "whisper-down", "backend": "whisper",
                      "engine": "whisper-server", "port": 9003, "host": "elsewhere"},
            "chatty": {"display_name": "chat-model", "backend": "openai", "port": 9100},
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")


# --------------------------------------------------------------------------- #
# fakes for the proxy round-trip
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {"content-type": "application/json"}


class _FakeClient:
    def __init__(self, handler):
        self._h = handler

    async def post(self, url, **kwargs):
        return self._h(url, kwargs)  # handler returns _FakeResp or raises


class _FakeReq:
    def __init__(self, body=b"----x\r\n", headers=None):
        self._body = body
        self.headers = headers or {"content-type": "multipart/form-data; boundary=x"}
        self.state = types.SimpleNamespace()

    async def body(self):
        return self._body


def _proxy(req):
    return asyncio.run(server_audio_asr._proxy_audio(
        req, ctx_path="/v1/audio/transcriptions"))


def _model_body(model_id: str) -> bytes:
    return b'Content-Disposition: form-data; name="model"\r\n\r\n' + model_id.encode() + b"\r\n"


def _req_with_ctx(body=b"----x\r\n"):
    req = _FakeReq(body=body)
    req.state.obs_ctx = ObservabilityCtx()
    return req


# --------------------------------------------------------------------------- #
# happy path — bytes forwarded to the single local backend
# --------------------------------------------------------------------------- #
def test_happy_path_forwards_to_local_whisper(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return _FakeResp(200, b'{"text":"wa"}')

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    resp = _proxy(_FakeReq())
    assert resp.status_code == 200 and b"wa" in resp.body
    assert len(calls) == 1 and ":9001" in calls[0]
    assert resp.headers["x-hub-requested-model"] == "audio_transcribe"
    assert resp.headers["x-hub-served-model"] == "wa"
    assert resp.headers["x-hub-served-host"] == "pc"


def test_unknown_model_name_still_addresses_the_backend(monkeypatch, write_config):
    """``whisper-1`` is the OpenAI SDK's default STT model name — not one of
    our ids, so it is not a strict model request."""
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    resp = _proxy(_FakeReq(body=_model_body("whisper-1")))
    assert resp.status_code == 200 and b"wa" in resp.body


def test_role_alias_is_case_insensitive(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    resp = _proxy(_FakeReq(body=_model_body("Audio_Transcribe")))
    assert resp.status_code == 200
    assert resp.headers["x-hub-served-model"] == "wa"


def test_capitalized_owned_id_is_still_that_model(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return _FakeResp(200, b'{"text":"wa"}')

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    req = _req_with_ctx(body=_model_body("Whisper-A"))   # display_name, capitalized
    resp = _proxy(req)
    assert resp.status_code == 200
    assert req.state.obs_ctx.model == "Whisper-A"        # echoed as the caller typed it
    assert req.state.obs_ctx.served_model == "wa"        # …resolved to the real row
    assert len(calls) == 1 and ":9001" in calls[0]


# --------------------------------------------------------------------------- #
# strict explicit model — no silent substitution (#412)
# --------------------------------------------------------------------------- #
def test_explicit_unavailable_model_is_503_never_another_model(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return _FakeResp(200, b'{"text":"served by wa"}')

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq(body=_model_body("wdown")))
    assert ei.value.status_code == 503
    assert "wdown" in ei.value.detail          # names the model that was asked for
    assert not calls                           # wa was never asked to stand in


def test_capitalized_unserveable_id_is_rejected_not_substituted(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return _FakeResp(200, b'{"text":"wa"}')

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq(body=_model_body("WDOWN")))
    assert ei.value.status_code == 503
    assert not calls


def test_explicit_non_audio_model_is_400(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq(body=_model_body("chatty")))
    assert ei.value.status_code == 400


def test_not_enabled_here_does_not_claim_an_outage(monkeypatch, write_config):
    """A configured row this host doesn't serve must not report an outage
    that isn't happening — distinct message for a distinct condition."""
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq(body=_model_body("wdown")))
    detail = ei.value.detail
    assert "not enabled on this host" in detail
    assert "Nothing is down" in detail   # explicitly disclaims an outage


def test_no_whisper_enabled_is_503(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {"pc": {"platform": "win32", "default": True, "enabled": []}},
        "models": {},
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")
    req = _req_with_ctx()
    with pytest.raises(HTTPException) as ei:
        _proxy(req)
    assert ei.value.status_code == 503
    assert req.state.obs_ctx.model == "audio_transcribe"
    assert req.state.obs_ctx.error_detail
    assert ei.value.headers["X-Hub-Requested-Model"] == "audio_transcribe"


# --------------------------------------------------------------------------- #
# upstream failures & pass-throughs
# --------------------------------------------------------------------------- #
def test_backend_down_is_distinct_503(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)

    def handler(url, kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq())
    assert ei.value.status_code == 503
    assert "not running on :9001" in ei.value.detail


def test_other_upstream_error_is_502(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)

    def handler(url, kwargs):
        raise httpx.ReadTimeout("mid-flight")

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    with pytest.raises(HTTPException) as ei:
        _proxy(_FakeReq())
    assert ei.value.status_code == 502


def test_client_error_passes_through(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(400, b'{"error":"bad audio"}')))
    resp = _proxy(_FakeReq())
    assert resp.status_code == 400
    assert b"bad audio" in resp.body


def test_upstream_404_survives_unchanged(monkeypatch, write_config):
    """An upstream status is *returned* as a Response, never raised, so it
    must not get caught by the observability funnel and re-flavoured as a
    whisper outage."""
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(404, b'{"error":"no such route"}')))
    resp = _proxy(_FakeReq())
    assert resp.status_code == 404
    assert b"no such route" in resp.body


# --------------------------------------------------------------------------- #
# observability: requested vs served (#412)
# --------------------------------------------------------------------------- #
def test_record_carries_requested_and_served(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    req = _req_with_ctx()
    resp = _proxy(req)
    assert resp.status_code == 200
    assert req.state.obs_ctx.model == "audio_transcribe"   # what was requested
    assert req.state.obs_ctx.backend == "whisper"
    assert req.state.obs_ctx.served_model == "wa"          # what actually served
    assert req.state.obs_ctx.served_host == "pc"


def test_strict_reject_is_recorded_not_blank(monkeypatch, write_config):
    """A rejection must be the *most* observable outcome, not the least
    (#412 F1): the ring row names what was asked for and why it failed, and
    the response carries the same header trio a 200 does."""
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    req = _req_with_ctx(body=_model_body("wdown"))
    with pytest.raises(HTTPException) as ei:
        _proxy(req)

    ctx = req.state.obs_ctx
    assert ctx.model == "wdown"          # the ring row names what was asked for
    assert ctx.backend == "whisper"      # …and which family it addressed
    assert ctx.served_model == ""        # nothing served it
    assert ctx.served_host == ""
    assert "wdown" in ctx.error_detail   # …and why it failed
    assert ei.value.headers["X-Hub-Requested-Model"] == "wdown"
    assert ei.value.headers["X-Hub-Served-Model"] == ""
    assert ei.value.headers["X-Hub-Served-Host"] == ""


def test_backend_down_does_not_claim_a_served_model(monkeypatch, write_config):
    """``served_model`` is written only once a model has answered (#412 F2)."""
    _single_whisper_config(write_config, monkeypatch)

    def handler(url, kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    req = _req_with_ctx()
    with pytest.raises(HTTPException):
        _proxy(req)
    assert req.state.obs_ctx.served_model == ""
    assert req.state.obs_ctx.served_host == ""
    assert req.state.obs_ctx.model == "audio_transcribe"


def test_local_backend_headers_are_not_trusted(monkeypatch, write_config):
    """A local whisper-server is not a hub; a stray X-Hub-* header from one
    must not override what this hub dispatched."""
    _single_whisper_config(write_config, monkeypatch)

    def handler(url, kwargs):
        return _FakeResp(200, b'{"text":"wa"}', headers={
            "content-type": "application/json",
            "X-Hub-Served-Model": "impostor",
            "X-Hub-Served-Host": "impostor-host",
        })

    monkeypatch.setattr(server_audio_asr, "get_async_client", lambda: _FakeClient(handler))
    req = _req_with_ctx()
    resp = _proxy(req)
    assert req.state.obs_ctx.served_model == "wa"
    assert req.state.obs_ctx.served_host == "pc"
    assert resp.headers["x-hub-served-model"] == "wa"
    assert resp.headers["x-hub-served-host"] == "pc"


# --------------------------------------------------------------------------- #
# header sanitation (#412 F5)
# --------------------------------------------------------------------------- #
def test_control_bytes_are_stripped_from_the_echoed_header(monkeypatch, write_config):
    """A `model=` carrying a NUL passes the CRLF-only body regex; writing it
    verbatim made h11 raise *after* the transcription had already run."""
    _single_whisper_config(write_config, monkeypatch)
    monkeypatch.setattr(
        server_audio_asr, "get_async_client",
        lambda: _FakeClient(lambda url, kwargs: _FakeResp(200, b'{"text":"wa"}')))
    resp = _proxy(_FakeReq(body=_model_body("wh\x00is\x7fper-1")))
    echoed = resp.headers["x-hub-requested-model"]
    assert echoed == "whisper-1"
    assert all(0x20 <= ord(c) <= 0x7e for c in echoed)


# --------------------------------------------------------------------------- #
# registry lookup asymmetry — resolve_any loose, resolve exact
# --------------------------------------------------------------------------- #
def test_resolve_any_is_case_insensitive_resolve_is_not(monkeypatch, write_config):
    _single_whisper_config(write_config, monkeypatch)
    assert model_registry.resolve_any("WHISPER-A").id == "wa"
    assert model_registry.resolve_any("  Wa  ").id == "wa"
    assert model_registry.resolve("WHISPER-A") is None   # exact-match, untouched
    assert model_registry.resolve("wa").id == "wa"
