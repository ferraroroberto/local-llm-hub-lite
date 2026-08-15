"""Models tab API — per-backend tile state + start/stop/force-stop/ping."""

from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException

from src import backend_process as bp
from src.http_client import get_async_client
from src.model_registry import (
    Model,
    enabled_models,
    resolve as resolve_model,
)
from src.server_process import (
    OWNERSHIP_EXTERNAL,
    OWNERSHIP_NONE,
    OWNERSHIP_OURS,
    snapshot_listening_pids,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _add_startup_fields(row: Dict[str, Any], m: Model) -> None:
    """Annotate a tile row with its declared startup policy — the registry
    fields the SPA's eager / on-demand badge renders.
    """
    row["startup"] = m.startup
    row["idle_unload_minutes"] = m.idle_unload_minutes
    row["est_vram_mb"] = m.est_vram_mb


def _require_model(model_id: str) -> Model:
    """Resolve ``model_id`` via ``bp.resolve_model_by_id`` or raise the same
    404 every start/stop/force-stop/log handler needs — the identical
    resolve-or-404 block each one opened with before this helper existed.
    """
    target = bp.resolve_model_by_id(model_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"model {model_id!r} not enabled")
    return target


def _ownership_from_snapshot(m: Model, listening: Dict[int, list]) -> tuple[str, Any]:
    """Compute (ownership, pid) for a controllable model from a port→pids map.

    Avoids the per-model netstat invocation that ``bp.ownership`` does
    when checking each model in isolation.
    """
    if bp.is_running(m.id):
        return OWNERSHIP_OURS, bp.pid(m.id)
    if not m.port:
        return OWNERSHIP_NONE, None
    pids = listening.get(m.port) or []
    if pids:
        return OWNERSHIP_EXTERNAL, pids[0]
    return OWNERSHIP_NONE, None


@router.get("/api/models")
async def list_models_for_admin() -> Dict[str, Any]:
    """Per-tile state for every enabled model — all rows are local; every
    model operates purely on this host's ``backend_process``.

    Two pieces are expensive: probing reachability over HTTP per backend
    (each costs up to 0.5 s) and resolving port → PID via netstat (one
    shell-out per call). We fan the HTTP probes out concurrently, and
    do a single netstat snapshot up front instead of one per backend —
    O(N) → O(1) subprocesses, O(N) → O(0.5 s) wall time when all
    backends are alive.
    """
    local_models = list(enabled_models())

    # psutil gives us every listening port in ~2 ms — use it both for
    # ownership *and* as a cheap reachability gate so we never fire an
    # HTTP probe at a port that isn't bound.
    listening = await asyncio.to_thread(snapshot_listening_pids)

    async def _probe_reach(m: Model) -> bool:
        if not m.port or m.port not in listening:
            # Port isn't bound → definitely not reachable; skip the
            # 1-second-per-dead-backend HTTP probe.
            return False
        return await asyncio.to_thread(bp.is_reachable, m, 0.4)

    reach_results = await asyncio.gather(*(_probe_reach(m) for m in local_models))

    rows: List[Dict[str, Any]] = []
    for m, reachable in zip(local_models, reach_results):
        # Virtual aliases share an existing backend's port and own no process,
        # so they're reachable but never independently start/stop-able.
        controllable = m.backend in ("openai", "whisper") and not m.virtual
        own = OWNERSHIP_NONE
        pid: Any = None
        if controllable:
            own, pid = _ownership_from_snapshot(m, listening)
        row: Dict[str, Any] = {
            "id": m.id,
            "display_name": m.display_name,
            "backend": m.backend,
            "engine": m.engine,
            "port": m.port,
            "url": m.url,
            "aliases": list(m.aliases or []),
            "controllable": controllable,
            "ownership": own,
            "pid": pid,
            "reachable": bool(reachable),
            "model_path": m.model_path,
        }
        _add_startup_fields(row, m)
        rows.append(row)

    return {"models": rows}


@router.post("/api/models/{model_id}/start")
async def model_start(model_id: str) -> Dict[str, Any]:
    target = _require_model(model_id)
    if target.virtual:
        raise HTTPException(
            status_code=400,
            detail=f"model {model_id!r} is a virtual alias of another backend — nothing to start",
        )
    if not (target.backend in ("openai", "whisper")):
        raise HTTPException(
            status_code=400,
            detail=f"backend {target.backend!r} has no managed process",
        )
    ok, msg = bp.start(model_id)
    if not ok:
        # "already running" is OK in the SPA — surface as 409 so the UI can ignore.
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "detail": msg}


@router.post("/api/models/{model_id}/stop")
async def model_stop(model_id: str) -> Dict[str, Any]:
    target = _require_model(model_id)
    ok, msg = bp.stop(model_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "detail": msg}


@router.post("/api/models/{model_id}/force-stop")
async def model_force_stop(model_id: str) -> Dict[str, Any]:
    """Force-kill whatever process holds this model's port.

    Use when the hub doesn't own the process — e.g. a stale backend
    from a previous tray session, or a llama-server someone started
    by hand. taskkill on Windows, SIGKILL on POSIX. The hub doesn't
    know what's listening — that's the whole point — so the caller
    is implicitly saying "I take responsibility for this PID".
    """
    target = _require_model(model_id)
    ok, msg = bp.force_stop_external(model_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "detail": msg}


@router.get("/api/models/{model_id}/log")
async def model_log(model_id: str, limit: int = 400) -> Dict[str, Any]:
    """Tail of a managed backend's log file (``data/logs/backend-<id>.log``).

    Readable for a backend the hub spawned *and* one it inherited across a
    restart — the child owns the log fd. Empty ``lines`` (200) when the
    backend has never started; 404 only for an unknown model.
    """
    target = _require_model(model_id)
    if not (target.backend in ("openai", "whisper")):
        raise HTTPException(
            status_code=400,
            detail=f"backend {target.backend!r} has no managed process",
        )
    limit = max(1, min(limit, bp.LOG_TAIL_LINES * 10))
    lines = await asyncio.to_thread(bp.log_lines, model_id, limit)
    return {
        "id": model_id,
        "lines": lines,
        "path": f"data/logs/backend-{model_id}.log",
    }


def _silent_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """A tiny mono 16-bit PCM WAV of silence, built in memory.

    Just enough for whisper-server to decode and return a (blank)
    transcription — proves the backend can actually run inference, not
    merely that its port is open. 0.5s (not the original 0.1s): FluidAudio's
    Parakeet worker (#138) rejects anything under ~0.3s as invalidAudioData;
    whisper.cpp tolerates any length so the longer default doesn't regress it.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


async def _probe(url: str, *, timeout: float, **kwargs: Any) -> tuple[Any, float, Optional[str]]:
    """POST ``url`` through the shared pooled client, timing the round trip.

    Both ``model_ping`` arms (whisper/chat) only differ in URL, payload
    shape, and timeout — this is that shared probe. Goes through
    ``get_async_client()`` rather than constructing a fresh ``httpx.AsyncClient``
    per call, which is exactly what that shared client exists to avoid (issue
    #165: ~0.26s per construction vs ~1ms reused).

    On success returns ``(response, latency_ms, None)``; on ``httpx.HTTPError``
    returns ``(None, latency_ms, str(exc))`` — callers shape the two cases into
    the identical ``{"ok": False, "status": 0, ...}`` payload themselves.
    """
    client = get_async_client()
    t0 = time.monotonic_ns()
    try:
        r = await client.post(url, timeout=timeout, **kwargs)
    except httpx.HTTPError as exc:
        return None, (time.monotonic_ns() - t0) / 1e6, str(exc)
    return r, (time.monotonic_ns() - t0) / 1e6, None


def _ping_result(r: Any, latency_ms: float) -> Dict[str, Any]:
    """Shape a backend probe response into the tile's ping payload."""
    body: Dict[str, Any] = {}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:300]}
    usage = body.get("usage") if isinstance(body, dict) else None
    return {
        "ok": r.is_success,
        "status": r.status_code,
        "latency_ms": round(latency_ms, 1),
        "usage": usage or {},
        "error": "" if r.is_success else (body.get("detail") if isinstance(body, dict) else str(r.status_code)),
    }


@router.post("/api/models/{model_id}/ping")
async def model_ping(model_id: str) -> Dict[str, Any]:
    """Probe the backend through the hub and report latency.

    Confirms the backend actually answers, not just that the port is open.
    The probe is protocol-aware: chat/ASR backends speak different APIs, so
    a chat ping at a whisper row would always 400. Whisper rows get a real
    audio transcription probe instead; everything else gets a 1-token chat
    probe.
    """
    target = bp.resolve_model_by_id(model_id)
    if target is None:
        # bp.resolve_model_by_id only matches the registry id; fall back to
        # the broader lookup (id, display_name, or alias) so pinging by an
        # alias like "agentic_light" still resolves.
        target = resolve_model(model_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown model {model_id!r}")

    from src.host_profile import hub_port

    port = hub_port()
    if target.backend == "whisper":
        # Whisper speaks the OpenAI audio API, not chat — send a tiny silent
        # clip to the hub's transcription proxy (model=display_name routes it
        # to this exact backend and keeps the hit in the observability ring).
        url = f"http://127.0.0.1:{port}/v1/audio/transcriptions"
        files = {"file": ("ping.wav", _silent_wav(), "audio/wav")}
        data = {"model": target.display_name}
        # Generous timeout: a lazy/CPU whisper backend may cold-load.
        r, latency_ms, error = await _probe(url, timeout=60.0, files=files, data=data)
        if error is not None:
            return {"ok": False, "status": 0, "latency_ms": latency_ms, "error": error}
        return _ping_result(r, latency_ms)

    url = f"http://127.0.0.1:{port}/v1/messages"
    payload = {
        "model": target.display_name,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    r, latency_ms, error = await _probe(url, timeout=20.0, json=payload)
    if error is not None:
        return {"ok": False, "status": 0, "latency_ms": latency_ms, "error": error}
    return _ping_result(r, latency_ms)
