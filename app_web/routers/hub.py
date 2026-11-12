"""Hub tab API — status, live request stream, log tail, install.

Endpoints (all under /admin/api/hub):
  * GET  /status            — pid, uptime, local/lan URLs, build identity
  * GET  /log/tail          — SSE stream of root-logger lines
  * GET  /log/recent        — non-SSE seed (last N lines)
  * GET  /stats             — 5-minute ring of RAM/CPU/GPU samples (sparklines)
  * GET  /requests/stream   — SSE stream of every routed /v1/* request
  * GET  /requests/recent   — non-SSE seed (last N records)
  * GET  /errors/recent     — non-2xx ring
  * GET  /counters          — per-backend counters since hub start

Plus /admin/api/install/{status,fix-all} for the install flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.hub_log import HUB_LOG
from src.hub_observability import OBS
from src.process.server_process import lan_ip

from ._helpers import maybe_json, sse_stream

logger = logging.getLogger(__name__)
router = APIRouter()


# ----------------------------------------------------------------- helpers

def _hub_port() -> int:
    from src.host_profile import hub_port

    return int(hub_port())


# ---------------------------------------------------------------- status

@router.get("/api/hub/status")
async def hub_status(request: Request) -> Dict[str, Any]:
    from src.host_profile import resolve as resolve_host

    port = _hub_port()
    lan = lan_ip()
    uptime_s = max(0.0, time.time() - OBS.started_at())
    return {
        "running": True,  # we ARE the hub — if you can read this, it's up
        "pid": os.getpid(),
        "port": port,
        "local_url": f"http://127.0.0.1:{port}",
        "lan_url": f"http://{lan}:{port}" if lan else "",
        "started_at": OBS.started_at(),
        "uptime_s": round(uptime_s, 1),
        "host": resolve_host().id,
    }


# ----------------------------------------------------------------- log tail

@router.get("/api/hub/log/recent")
async def log_recent(limit: int = 400) -> Dict[str, Any]:
    return {"lines": HUB_LOG.lines(limit=max(1, min(limit, 2000)))}


@router.get("/api/hub/log/tail")
async def log_tail(request: Request) -> StreamingResponse:
    return sse_stream(
        request, HUB_LOG.subscribe, HUB_LOG.unsubscribe,
        seed=HUB_LOG.lines(limit=200),
    )


# ----------------------------------------------------------------- requests

@router.get("/api/hub/requests/recent")
async def requests_recent(limit: int = 50) -> Dict[str, Any]:
    return {"requests": OBS.recent_requests(limit=max(1, min(limit, 200)))}


@router.get("/api/hub/requests/stream")
async def requests_stream(request: Request) -> StreamingResponse:
    from src.hub_observability import _rec_to_dict

    return sse_stream(
        request, OBS.subscribe, OBS.unsubscribe,
        seed=OBS.recent_requests(limit=20),
        to_dict=_rec_to_dict,
        reverse_seed=True,  # send oldest-first so order matches the stream
    )


@router.get("/api/hub/errors/recent")
async def errors_recent(limit: int = 50) -> Dict[str, Any]:
    return {"errors": OBS.recent_errors(limit=max(1, min(limit, 50)))}


@router.get("/api/hub/counters")
async def counters() -> Dict[str, Any]:
    return {"counters": OBS.counters_snapshot()}


# ----------------------------------------------------------------- stats

@router.get("/api/hub/stats")
async def stats() -> Dict[str, Any]:
    """gpu_stats() shells out to nvidia-smi (3s timeout). Keep it off the
    event loop so the rest of /admin stays snappy."""
    from src import system_stats

    ram = system_stats.ram_stats()
    cpu = system_stats.cpu_stats()
    gpus = await asyncio.to_thread(system_stats.gpu_stats)
    history = OBS.stats_snapshot()
    return {"ram": ram, "cpu": cpu, "gpus": gpus, "history": history}


# ----------------------------------------------------------------- install

@router.get("/api/install/status")
async def install_status() -> Dict[str, Any]:
    """Run every install check off the event loop — many shell out to
    ``nvidia-smi`` / ``llama-server --version`` / ``whisper-server --version``
    via blocking subprocess.run, which would otherwise pin the entire
    uvicorn worker for seconds while other admin requests queue up."""
    from src import install

    report = await asyncio.to_thread(install.run_all_checks)
    return {
        "worst_status": report.worst_status,
        "ok": report.ok,
        "checks": [asdict(c) for c in report.checks],
    }


@router.post("/api/install/fix")
async def install_fix(request: Request) -> Dict[str, Any]:
    """Run a single fix by ``fix_id``.

    Uses the brief use_cache=True report: the admin UI always
    calls install_status() — which populates that cache — moments before a
    user clicks a fix button, so locating one check by fix_id doesn't need
    to force a second full (expensive) battery run.
    """
    from src import install

    body = await maybe_json(request)
    fix_id = (body or {}).get("fix_id")
    if not fix_id:
        raise HTTPException(status_code=400, detail="fix_id is required")
    report = await asyncio.to_thread(install.run_all_checks, use_cache=True)
    target = next((c for c in report.checks if c.fix_id == fix_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no fixable check with fix_id={fix_id!r}")
    fn = install.fix_fn_for(target)
    if fn is None:
        raise HTTPException(status_code=400, detail=f"no fix function for {fix_id!r}")
    try:
        await asyncio.to_thread(fn)
    except Exception as exc:  # noqa: BLE001 — surface the failure to the UI
        raise HTTPException(status_code=500, detail=f"fix {fix_id!r} failed: {exc}")
    return {"ok": True, "fix_id": fix_id}


@router.post("/api/install/fix-all")
async def install_fix_all() -> Dict[str, Any]:
    """Run every currently-fixable check. Same brief use_cache=True reuse
    as install_fix() — see its docstring."""
    from src import install

    report = await asyncio.to_thread(install.run_all_checks, use_cache=True)
    ran: List[Dict[str, Any]] = []
    for c in report.checks:
        if c.status not in ("missing", "error"):
            continue
        fn = install.fix_fn_for(c)
        if fn is None:
            continue
        try:
            await asyncio.to_thread(fn)
            ran.append({"fix_id": c.fix_id, "ok": True})
        except Exception as exc:  # noqa: BLE001
            ran.append({"fix_id": c.fix_id, "ok": False, "error": str(exc)})
    return {"ran": ran}
