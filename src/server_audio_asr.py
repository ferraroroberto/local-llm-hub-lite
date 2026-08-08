"""Whisper ASR proxy routes (``/v1/audio/transcriptions``, ``/health``).

Lite fork: one local whisper-server, no role chains, no failover, no
glossary. The whisper-server already speaks the OpenAI ``/v1/audio/*``
shape, so the hub mostly forwards bytes — the point of routing through
here (rather than hitting the backend port directly) is that the
observability middleware records the request in the live ring.

Routes are collected on a module-level :class:`fastapi.APIRouter` and mounted
onto the parent hub app by ``server.py`` via ``include_router``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .http_client import get_async_client
from .model_registry import Model
from .server_audio_common import (
    HEADER_REQUESTED_MODEL,
    HEADER_SERVED_HOST,
    HEADER_SERVED_MODEL,
    _audio_upstream_error,
    _header_safe,
)
from .server_common import ensure_backend_ready_or_503

logger = logging.getLogger(__name__)

router = APIRouter()

# Role alias a caller may send as ``model=`` to address "whatever this hub's
# transcription model is" — kept for compatibility with clients written
# against the full hub's role addressing.
_ROLE_ALIASES = {"audio_transcribe"}


def _local_whisper() -> Optional[Model]:
    """The single locally-enabled whisper backend, or ``None``."""
    from .model_registry import local_models

    for m in local_models():
        if m.backend == "whisper" and m.port:
            return m
    return None


def _whisper_model_for_request(model_name: str) -> Model:
    """Resolve the whisper backend for a request (single-backend edition).

    * No ``model=``, a role alias (``audio_transcribe``), or a name the
      registry has never heard of (OpenAI clients must send something, and
      the SDK's default is ``whisper-1``) → the single local whisper model.
    * An explicit **known** model id/alias (matched case-insensitively,
      #412) must actually be the whisper model — a known id this hub cannot
      serve gets a distinct 400/503 rather than being silently answered by
      a different model.

    Raises ``HTTPException``: 503 when no whisper backend is enabled here,
    400 when the named model is not a transcription backend, 503 when it is
    a configured transcription model this host does not serve.
    """
    from .model_registry import resolve as _resolve_model, resolve_any

    local = _local_whisper()

    if model_name and model_name.strip().lower() not in _ROLE_ALIASES:
        known = resolve_any(model_name)
        if known is not None:
            serveable = _resolve_model(known.id)
            if serveable is not None and serveable.backend == "whisper" and serveable.port:
                return serveable
            if known.backend != "whisper" or not known.port:
                logger.warning(
                    "⚠️ audio request rejected: %s is a '%s' model, not a "
                    "transcription backend",
                    known.id, known.backend,
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"model '{model_name}' is not a transcription backend "
                        f"(it is a '{known.backend}' model) — /v1/audio/* only "
                        f"serves whisper-shaped models"
                    ),
                )
            logger.warning(
                "⚠️ audio request rejected: %s is a configured model this host "
                "does not serve — refusing to substitute another model",
                known.id,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"model '{model_name}' is not enabled on this host. "
                    f"Nothing is down: this hub simply does not serve that "
                    f"model, and an explicit model id is never answered by a "
                    f"different one. Address the role ('audio_transcribe', or "
                    f"omit model) to use this hub's transcription model."
                ),
            )
        # Unknown to the registry → a client-side placeholder, not a model
        # request: fall through to the single local backend.

    if local is None:
        raise HTTPException(status_code=503, detail="no whisper backend enabled on this host")
    return local


def _observable_audio_error(exc: HTTPException, ctx, requested: str) -> HTTPException:
    """Make an audio *failure* as observable as an audio success (#412).

    A strict rejection is precisely the event an operator has to be able to
    see, yet it used to be the least visible thing the hub emitted: the reject
    fired before anything stamped ``request.state.obs_ctx``, so the ring
    recorded a blank row and ``ObservatoryMiddleware`` could not recover it —
    its fallback peeks a *JSON* body and a multipart request never parses.
    Stamp the detail on the context and echo the same header trio a 200
    carries, with the served pair empty because nothing served.
    """
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if ctx is not None and not getattr(ctx, "error_detail", ""):
        ctx.error_detail = detail
    headers = dict(exc.headers or {})
    headers.setdefault(HEADER_REQUESTED_MODEL, _header_safe(requested))
    headers.setdefault(HEADER_SERVED_MODEL, _header_safe(getattr(ctx, "served_model", "") or ""))
    headers.setdefault(HEADER_SERVED_HOST, _header_safe(getattr(ctx, "served_host", "") or ""))
    exc.headers = headers
    return exc


async def _proxy_audio(request: Request, *, ctx_path: str) -> Response:
    """Forward a multipart audio request to the local whisper backend.

    The whisper-server already speaks the OpenAI ``/v1/audio/*`` shape, so we
    forward the bytes and pass the response back — the point of going through
    the hub is the observability ring. Who served is stamped on the request
    record and echoed in the response headers (#412).
    """
    body = await request.body()

    # Peek the ``model`` field out of the multipart body to choose a
    # backend. python-multipart parsing is overkill — the field shows
    # up as ``Content-Disposition: form-data; name="model"`` followed
    # by a couple of CRLF lines and the value. Best-effort regex.
    #
    # Scan the first 16 KB first (the cheap path — standard SDK clients
    # serialize plain form fields before the file part, so ``model``
    # lands in the head). Fall back to the whole body if it's not there:
    # a client that puts a large file *before* the model field would
    # otherwise misroute (#128).
    model_name = ""
    try:
        pattern = rb'name="model"\r?\n\r?\n([^\r\n]+)'
        match = re.search(pattern, body[: 16 * 1024]) or re.search(pattern, body)
        if match:
            # Sanitize at the source: this value is echoed back in a response
            # header and the pattern above only excludes CR/LF, so a control
            # byte would otherwise reach h11 (see :func:`_header_safe`).
            model_name = _header_safe(
                match.group(1).decode("ascii", errors="ignore").strip()
            )
    except Exception:  # noqa: BLE001
        pass

    # Stamp the observability context *before* anything below can raise (#412).
    # A strict rejection is the event that most needs to be visible, and the
    # middleware cannot fill these in for a multipart request.
    ctx = getattr(request.state, "obs_ctx", None)
    requested = model_name or "audio_transcribe"
    if ctx is not None:
        ctx.model = requested
        ctx.backend = "whisper"

    try:
        return await _dispatch_audio(
            request, body=body, model_name=model_name, requested=requested,
            ctx_path=ctx_path, ctx=ctx,
        )
    except HTTPException as exc:
        raise _observable_audio_error(exc, ctx, requested)


async def _dispatch_audio(
    request: Request, *, body: bytes, model_name: str, requested: str,
    ctx_path: str, ctx,
) -> Response:
    """Resolve the target, then POST the raw bytes to the whisper backend.

    Split out of :func:`_proxy_audio` purely so every ``HTTPException``
    raised on the way is funnelled through one observability wrapper
    instead of each raise site remembering to stamp the context (#412).
    """
    import httpx as _httpx

    target = _whisper_model_for_request(model_name)

    # On-demand lifecycle (#422): a cold ``startup: on_demand`` whisper
    # backend is spawned here and the request blocks until it answers
    # (distinct 503 on load failure). No-op for eager rows. Off-loop —
    # ensure_ready blocks polling readiness.
    import asyncio as _asyncio
    await _asyncio.to_thread(ensure_backend_ready_or_503, target)

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in {"content-type", "accept"}
    }
    url = f"http://127.0.0.1:{target.port}{ctx_path}"
    timeout = _httpx.Timeout(30.0, read=300.0, write=60.0, pool=10.0)
    client = get_async_client()
    try:
        upstream = await client.post(
            url, content=body, headers=fwd_headers or None, timeout=timeout,
        )
    except _httpx.HTTPError as exc:
        raise _audio_upstream_error(exc, backend="whisper-server", port=target.port)

    # Who served (#412): the single local backend, on the active host.
    served_model = target.id
    try:
        from .host_profile import resolve as _resolve_host
        served_host = _resolve_host().id
    except Exception:  # noqa: BLE001 — never fail a served request over a label
        served_host = ""
    if ctx is not None:
        ctx.served_model = served_model
        ctx.served_host = served_host

    _drop = {
        "content-length", "transfer-encoding", "connection",
        HEADER_REQUESTED_MODEL.lower(), HEADER_SERVED_MODEL.lower(),
        HEADER_SERVED_HOST.lower(),
    }
    out_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _drop
    }
    out_headers[HEADER_REQUESTED_MODEL] = _header_safe(requested)
    out_headers[HEADER_SERVED_MODEL] = _header_safe(served_model)
    out_headers[HEADER_SERVED_HOST] = _header_safe(served_host)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=out_headers,
    )


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request) -> Response:
    """Proxy transcription requests through the hub so they land in the
    observability ring. Clients that point directly at the backend port
    still work but are invisible to the admin UI — pointing at the hub
    here makes them visible without changing the request shape.
    """
    return await _proxy_audio(request, ctx_path="/v1/audio/transcriptions")


@router.get("/v1/audio/health")
def audio_health() -> Response:
    """Probe-only liveness of the audio backend — lets a consumer preflight
    instead of discovering an outage one failed transcription at a time (#147).

    Reports the enabled whisper backend with its port and whether it is
    currently reachable (a cheap GET to the backend, never a transcription).
    ``status`` is ``ok`` when it answers, ``degraded`` when it is down, and
    ``none`` when no whisper backend is enabled on this host. A degraded/none
    result returns HTTP 503 so a consumer can branch on the status code
    alone; ``ok`` returns 200.

    Defined as a sync route on purpose: ``is_reachable`` does blocking socket
    probes, so FastAPI runs it in a threadpool rather than stalling the loop.
    """
    import json as _json

    from .backend_process import is_reachable

    backends = []
    target = _local_whisper()
    if target is not None:
        backends.append({
            "id": target.id,
            "backend": target.backend,
            "port": target.port,
            "reachable": is_reachable(target, timeout=1.0),
        })

    if not backends:
        status, code = "none", 503
    elif all(b["reachable"] for b in backends):
        status, code = "ok", 200
    else:
        status, code = "degraded", 503

    return Response(
        content=_json.dumps({"status": status, "backends": backends}),
        status_code=code,
        media_type="application/json",
    )
