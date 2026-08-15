"""Local LLM hub (lite): Anthropic-compatible and OpenAI-compatible endpoints.

Each request resolves its `model` field against `config/models.yaml`
(by registry id, display_name, or alias) and routes to a local
llama-server backend (chat) or whisper-server (audio).

Two shapes exposed:
  * POST /v1/messages          - Anthropic shape (drop-in for the SDK)
  * POST /v1/chat/completions  - OpenAI shape (passthrough/translation)
  * GET  /v1/models            - union of enabled names (both shapes)

Caveats: local llama-server backends are text-only and 400 on image
input. Streaming: ``/v1/chat/completions`` proxies upstream SSE through
(with ``<think>`` blocks stripped for reasoning models); ``/v1/messages``
still returns a single JSON for ``stream=true``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from .anthropic_errors import install_anthropic_error_handlers
from .auth_middleware import ParentBearerTokenMiddleware
from .chat_translation import MessagesRequest, _run_openai_backend
from .cors_policy import install_cors
from .host_profile import hub_bind_host, hub_port
from .hub_log import install_root_handler
from .hub_observability import ObservatoryMiddleware
from .model_registry import Model, enabled_models
from .server_common import ensure_backend_ready_or_503 as _ensure_backend_ready, resolve_model_or_400 as _resolve
from . import on_demand as _on_demand
from .server_audio_asr import router as _audio_router
from .openai_upstream import (
    UpstreamError,
    call_openai_chat,
    call_openai_chat_stream,
    clean_openai_response,
    iter_cleaned_sse,
)
from .trace_id_middleware import TraceIdHeaderMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
# Wire the in-memory ring handler so the admin webapp's Hub tab can tail
# both our logs and uvicorn's (access + error) without re-reading stdout.
install_root_handler()
logger = logging.getLogger(__name__)


# ---- response-shape translation (endpoint-local; per-backend request
# translation and dispatch lives in chat_translation.py) ----

def _envelope_to_anthropic(env: Dict[str, Any], requested_model: str) -> Dict[str, Any]:
    text = env.get("result") or ""
    usage_raw = env.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": requested_model,
        "stop_reason": env.get("stop_reason") or "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage_raw.get("input_tokens", 0) or 0),
            "output_tokens": int(usage_raw.get("output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                usage_raw.get("cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(
                usage_raw.get("cache_read_input_tokens", 0) or 0
            ),
        },
    }


# ---- FastAPI app ----

app = FastAPI(title="Local LLM Hub Lite", version="0.3.0")
# Errors raised on the Anthropic-shape routes serialise as Anthropic's
# {"type": "error", "error": {...}} envelope instead of FastAPI's
# {"detail": ...} (#460), so the anthropic SDK's typed exceptions and
# retry logic behave against the hub as they do against the real API.
# Path-scoped: /v1/chat/completions keeps its existing shape.
install_anthropic_error_handlers(app)
# Observability middleware records every /v1/messages + /v1/chat/completions
# call into an in-memory ring read by the admin webapp's Hub tab. Volatile
# by design.
app.add_middleware(ObservatoryMiddleware)


# Bearer-token gate on the parent app. Loopback callers bypass; non-
# loopback callers must present the token (or be in the configured
# extra_allowlist). The /admin sub-app has its own copy of this
# middleware — its prefix is exempted here so a single auth boundary
# governs the whole process.
def _hub_get_token() -> str:
    """Resolve the bearer token from config/webapp_config.json on every
    check so the user can edit it without restarting the hub."""
    try:
        from .webapp_config import load_webapp_config
        return getattr(load_webapp_config(), "auth_token", "") or ""
    except Exception:  # noqa: BLE001
        return ""


app.add_middleware(ParentBearerTokenMiddleware, get_token=_hub_get_token)

# X-Trace-Id contract — accept client-supplied UUID4 / hex in, echo a
# deterministic trace ID out. Pure-ASGI middleware; added last so it
# sits OUTERMOST.
app.add_middleware(TraceIdHeaderMiddleware)


# Expose the same WebappConfig to the parent app so the middleware can
# read ``extra_allowlist`` without re-loading on every request. Note the
# token itself is *not* cached — we always re-read so the user can
# rotate without restarting.
try:
    from .webapp_config import load_webapp_config as _load_wcfg
    app.state.webapp_config = _load_wcfg()
except Exception as _exc:  # noqa: BLE001
    logger.warning("⚠️ could not load webapp_config: %s", _exc)

# CORS for browser-based clients (#462) — added LAST so it sits outside
# every layer above, including the bearer gate. A preflight OPTIONS
# carries no Authorization header by the browser's design, so it has to
# be answered here rather than 401'd downstream; real requests still
# travel the full stack and meet the gate unchanged. Loopback origins are
# allowed by default, extra origins are named in webapp_config.json, and
# a wildcard never ships — the policy and its rationale live in
# src/cors_policy.py.
install_cors(app, getattr(app.state, "webapp_config", None))


# Startup/shutdown handlers + the background resource sampler live in
# server_lifecycle.py (issue #198) — server.py stays app construction +
# route registration. ``_stop_backend_children`` is re-exported under its
# original name since tests/test_restart_keepalive.py calls it directly.
from .server_lifecycle import (  # noqa: E402
    register as _register_lifecycle,
    stop_backend_children as _stop_backend_children,
)

_register_lifecycle(app)


# Mount the admin sub-app at /admin. Done at import time so a fresh
# uvicorn workers picks it up; the sub-app has its own bearer-token
# middleware, separate from the parent hub.
def _mount_admin() -> None:
    # Guard against double-mount when the module is imported twice (e.g.
    # `python -m src.server` loads us as ``__main__`` and uvicorn then
    # re-imports as ``src.server`` to resolve the ``src.server:app``
    # spec).
    if any(getattr(r, "name", None) == "admin" for r in app.routes):
        return
    try:
        from app_web import create_app as _create_admin
        admin_app = _create_admin()
        admin_app.state.parent = app
        app.mount("/admin", admin_app, name="admin")
        logger.info("ℹ️ /admin sub-app mounted")
    except Exception as exc:  # noqa: BLE001
        logger.error("⚠️ /admin sub-app failed to mount: %s", exc)


_mount_admin()


@app.get("/", include_in_schema=False)
def root() -> Response:
    # Old landing page is gone — / now redirects to the admin webapp.
    return RedirectResponse(url="/admin/", status_code=307)


@app.get("/info", include_in_schema=False)
def info() -> Dict[str, Any]:
    return {
        "name": "Local LLM Hub Lite",
        "version": app.version,
        "description": "Local hub: Anthropic-shape + OpenAI-shape over llama-server backends.",
        "endpoints": {
            "health": "GET /health",
            "audio_health": "GET /v1/audio/health",
            "messages": "POST /v1/messages",
            "chat_completions": "POST /v1/chat/completions",
            "models": "GET /v1/models",
            "docs": "GET /docs",
        },
        "models": sorted({m.display_name for m in enabled_models()}),
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def list_models() -> Dict[str, Any]:
    data = []
    for m in enabled_models():
        for name in m.all_names:
            data.append({
                "id": name,
                "object": "model",
                "owned_by": m.backend,
                "backend": m.backend,
            })
    return {"object": "list", "data": data}


def _reject_non_chat_backend(model: Model, requested_name: str) -> Optional[HTTPException]:
    """Return the 400 to raise when a chat route is hit with an ASR backend.

    The whisper backend doesn't serve chat completions; both chat routes
    (/v1/messages and /v1/chat/completions) reject it with the same
    "POST to the right audio endpoint instead" 400. Returns ``None`` for
    any chat-capable backend so the caller can fall through to its normal
    handling.
    """
    if model.backend == "whisper":
        return HTTPException(
            status_code=400,
            detail=(
                f"{requested_name!r} is an ASR backend, not a chat model. "
                f"POST audio to http://127.0.0.1:{model.port}/v1/audio/transcriptions instead."
            ),
        )
    return None


@app.post("/v1/messages")
def messages(req: MessagesRequest, request: Request) -> JSONResponse:
    if req.stream:
        logger.warning("stream=true requested - returning non-streaming response")

    model = _resolve(req.model)
    ctx = getattr(request.state, "obs_ctx", None)
    if ctx is not None:
        ctx.backend = model.backend
    logger.info("/v1/messages model=%s backend=%s", req.model, model.backend)

    if model.backend == "openai":
        env = _run_openai_backend(model, req)
    elif (reject := _reject_non_chat_backend(model, req.model)) is not None:
        raise reject
    else:
        raise HTTPException(status_code=500, detail=f"unknown backend {model.backend!r}")

    payload = _envelope_to_anthropic(env, req.model)
    u = payload["usage"]
    if ctx is not None:
        ctx.in_tok = int(u["input_tokens"])
        ctx.out_tok = int(u["output_tokens"])
        ctx.cache_read_tok = int(u["cache_read_input_tokens"])
        ctx.cache_write_tok = int(u["cache_creation_input_tokens"])
        ctx.stop_reason = str(payload.get("stop_reason") or "")
    logger.info(
        "<- in=%d out=%d (cache_r=%d cache_w=%d) stop=%s backend=%s",
        u["input_tokens"], u["output_tokens"],
        u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
        payload["stop_reason"], model.backend,
    )
    return JSONResponse(payload)


# ---- OpenAI-shape endpoint ----

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    chat_template_kwargs: Optional[Dict[str, Any]] = None


def _build_openai_extra(model: Model, req: "ChatCompletionRequest") -> Dict[str, Any]:
    """Build the upstream payload overlay for an OpenAI-shape backend call.

    Seeds from the model's server-side ``inject_extra`` (e.g. the no-think
    alias's ``chat_template_kwargs``), then layers caller-sent fields on top
    so the caller always wins. Shared by the streaming and non-streaming
    ``openai`` backend paths so both build the same overlay the same way.
    """
    extra: Dict[str, Any] = dict(model.inject_extra or {})
    if req.tools is not None:
        extra["tools"] = req.tools
    if req.tool_choice is not None:
        extra["tool_choice"] = req.tool_choice
    if req.response_format is not None:
        extra["response_format"] = req.response_format
    if req.chat_template_kwargs is not None:
        extra["chat_template_kwargs"] = req.chat_template_kwargs
    return extra


def _stream_openai_passthrough(
    model: Model,
    req: "ChatCompletionRequest",
) -> StreamingResponse:
    """Proxy llama-server SSE through the hub, stripping ``<think>`` blocks.

    The upstream already speaks OpenAI-compatible SSE. We re-emit each
    line verbatim except ``data:`` frames whose JSON payload we mutate
    to fold ``reasoning_content`` and remove ``<think>...</think>``
    spans (using a per-stream :class:`ThinkStripper` so a tag split
    across chunks is still recognised).
    """
    base_url = model.url
    if not base_url:
        raise HTTPException(status_code=500, detail="model has no url")
    extra = _build_openai_extra(model, req)
    # On-demand idle tracking (#422): a locally-served on_demand model must
    # not be idle-unloaded while a stream is in flight — pair the start here
    # with the finish in the generator's ``finally`` (which also runs on a
    # client disconnect, via GeneratorExit).
    track = _on_demand.tracking(model).start()

    def event_stream() -> Any:
        import json as _json

        try:
            raw = call_openai_chat_stream(
                base_url,
                model=model.display_name,
                messages=req.messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                extra=extra or None,
            )
            for cleaned in iter_cleaned_sse(raw):
                yield cleaned + "\n"
            # SSE record terminator after the final line. llama-server
            # already sends ``data: [DONE]``; the trailing blank line
            # closes the last event for strict SSE parsers.
            yield "\n"
        except UpstreamError as e:
            logger.error("upstream stream error: %s", e)
            err = {
                "error": {
                    "message": str(e),
                    "type": "upstream_error",
                    "code": "upstream_error",
                }
            }
            yield "data: " + _json.dumps(err) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            track.finish()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest, request: Request) -> Response:
    model = _resolve(req.model)
    ctx = getattr(request.state, "obs_ctx", None)
    if ctx is not None:
        ctx.backend = model.backend
    logger.info(
        "/v1/chat/completions model=%s backend=%s stream=%s",
        req.model, model.backend, req.stream,
    )

    if model.backend == "openai":
        # On-demand lifecycle (#422): a cold ``startup: on_demand`` local
        # backend is spawned here and the request blocks until it answers
        # (distinct 503 on load failure). No-op for eager/virtual rows.
        _ensure_backend_ready(model)

    if req.stream and model.backend == "openai":
        return _stream_openai_passthrough(model, req)
    if req.stream:
        # Non-openai backends don't have an SSE source; fall back to a
        # single non-streaming response. Logged so it's visible.
        logger.warning(
            "stream=true on backend=%s - returning non-streaming response",
            model.backend,
        )

    reject = _reject_non_chat_backend(model, req.model)
    if reject is not None:
        raise reject

    if model.backend == "openai":
        base_url = model.url
        if not base_url:
            raise HTTPException(status_code=500, detail="model has no url")
        extra = _build_openai_extra(model, req)
        # On-demand idle tracking (#422) — see _stream_openai_passthrough.
        try:
            with _on_demand.tracking(model):
                raw = call_openai_chat(
                    base_url,
                    model=model.display_name,
                    messages=req.messages,
                    max_tokens=req.max_tokens,
                    temperature=req.temperature,
                    extra=extra or None,
                )
        except UpstreamError as e:
            raise HTTPException(status_code=502, detail=str(e))
        cleaned = clean_openai_response(raw)
        usage = cleaned.get("usage") or {}
        if ctx is not None:
            ctx.in_tok = int(usage.get("prompt_tokens", 0) or 0)
            ctx.out_tok = int(usage.get("completion_tokens", 0) or 0)
        # Passthrough of upstream response (already OpenAI-shape), with
        # <think>...</think> stripped from message.content and
        # reasoning_content folded into content when content is empty.
        return JSONResponse(cleaned)

    raise HTTPException(status_code=500, detail=f"unknown backend {model.backend!r}")


# ---- Audio routes (sibling module) ----
# The /v1/audio/transcriptions proxy lives in server_audio_asr.py — a plain
# APIRouter mounted here so the auth boundary and the observability
# middleware still cover it. See that module for the handler bodies.
app.include_router(_audio_router)


def main() -> None:
    import uvicorn
    from src.event_loop import LOOP_FACTORY
    uvicorn.run(
        "src.server:app",
        host=hub_bind_host(),
        port=hub_port(),
        reload=False,
        loop=LOOP_FACTORY,
    )


if __name__ == "__main__":
    main()
