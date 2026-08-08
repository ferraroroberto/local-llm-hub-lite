"""FastAPI startup/shutdown lifecycle wiring + the background resource
sampler for the hub app.

Extracted out of ``server.py`` (issue #198) — startup/shutdown event bodies
and the 2s-tick resource sampler live here so ``server.py`` stays app
construction + route registration.

``server.py`` wires these on with ``app.add_event_handler(...)`` (the
non-decorator form of ``@app.on_event``) rather than decorating them here,
so the functions stay plain, directly-callable, and directly-testable —
``tests/test_restart_keepalive.py`` calls ``stop_backend_children()``
straight, with no FastAPI app in the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .hub_log import HUB_LOG
from .hub_observability import OBS

logger = logging.getLogger(__name__)

# Tasks spawned by wire_observatory_loop, tracked so stop_background_tasks
# can cancel them on shutdown -- see that function's docstring for why.
_BACKGROUND_TASKS: list[asyncio.Task] = []


async def stop_background_tasks() -> None:
    """Cancel every perpetual task ``wire_observatory_loop`` spawned.

    On a real process exit this wouldn't matter (the OS reclaims everything),
    but the ASGI lifespan also tears down on every ``with TestClient(app) as
    c:`` exit in the test suite -- a still-running loop task would otherwise
    keep mutating module-global state in a torn-down loop (issue #416)."""
    tasks, _BACKGROUND_TASKS[:] = list(_BACKGROUND_TASKS), []
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def stop_backend_children() -> None:
    """Tear down every model subprocess the hub spawned.

    The hub owns its backend children (since the tray drives them via
    the admin API). Without this, a clean ``CTRL+C`` would leave
    orphan ``llama-server`` / ``whisper-server`` processes holding
    their ports until the user logged out.

    Exception: on an admin **restart** the children must survive so the
    respawned hub re-adopts them (``inherit_running_backends``). The
    restart endpoint sets ``backend_process.restart_pending()`` before
    signalling shutdown; we honour it by skipping teardown.
    """
    from . import backend_process as bp
    from . import http_client

    try:
        await http_client.aclose()
        http_client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("shutdown: closing shared httpx clients raised: %s", exc)

    if bp.restart_pending():
        survivors = list(bp.running_backends().keys())
        logger.info(
            "shutdown: restart in progress — leaving %d backend(s) running for adoption: %s",
            len(survivors), survivors,
        )
        return

    for model_id in list(bp.running_backends().keys()):
        try:
            ok, msg = bp.stop(model_id)
            logger.info("shutdown: stop %s -> %s %s", model_id, ok, msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shutdown: stop %s raised: %s", model_id, exc)


async def wire_observatory_loop() -> None:
    """Capture the running event loop so the synchronous middleware can
    fan out SSE events from non-async callers."""
    loop = asyncio.get_running_loop()
    OBS.attach_loop(loop)
    HUB_LOG.attach_loop(loop)
    # Start the resource sampler. 2s tick × 150 samples = 5 min ring.
    _BACKGROUND_TASKS.append(loop.create_task(_resource_sampler()))

    # Inherit any backend process left running on one of our ports by a
    # previous hub instance. Without this, every hub restart shows the
    # surviving model backends as "adopted" rather than "running".
    try:
        from . import backend_process as bp
        inherited = await asyncio.to_thread(bp.inherit_running_backends)
        if inherited:
            logger.info("📎 Inherited %d running backend(s) from a previous hub", inherited)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inherit_running_backends failed: %s", exc)

    # The hub owns configured backend autostart so every launch surface
    # (tray, run_hub.bat, python -m src.run_backend hub) behaves the same.
    _BACKGROUND_TASKS.append(loop.create_task(_autostart_configured_backends()))
    # On-demand idle watchdog (issue #422): periodically unloads a
    # ``startup: on_demand`` model that has sat idle past its
    # ``idle_unload_minutes`` window. Cheap when no row opts in — the pass
    # scans the registry and finds no candidates.
    _BACKGROUND_TASKS.append(loop.create_task(_on_demand_idle_loop()))


async def _autostart_configured_backends() -> None:
    # The registry-derived desired set: eager rows launchable on this host.
    from . import backend_process as bp
    from .model_registry import desired_model_ids

    model_ids = desired_model_ids()
    if not model_ids:
        return
    logger.info("autostart: configured backend set: %s", model_ids)
    for model_id in model_ids:
        try:
            ok, msg = await asyncio.to_thread(bp.start, model_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("autostart: %s raised: %s", model_id, exc)
            continue
        if ok or "already running" in msg.lower():
            logger.info("autostart: %s -> %s", model_id, msg)
        else:
            logger.warning("autostart: %s -> %s", model_id, msg)


async def _on_demand_idle_loop() -> None:
    """Idle unload for ``startup: on_demand`` models (issue #422).

    Thin lifecycle shim — the sweep cadence, the idle decision, and the
    in-flight guard live in ``src.on_demand`` (``idle_unload_loop``),
    keeping the policy unit-testable with no FastAPI app in the loop.
    """
    from . import on_demand

    try:
        await on_demand.idle_unload_loop()
    except Exception as exc:  # noqa: BLE001 — the shim must not crash startup
        logger.warning("on-demand idle loop exited abnormally: %s", exc)


async def _resource_sampler() -> None:
    """Background task that samples RAM + CPU + GPU usage every 2 s.

    The sampling itself runs in a worker thread (#392): ``gpu_stats()``
    shells out to nvidia-smi, which stalls for seconds when the GPU is
    busy serving models — calling it inline here blocked the whole event
    loop every 2 s, and any HTTP client with a 5 s timeout (the e2e
    suite's httpx calls) would intermittently trip on an otherwise-idle
    endpoint. Same off-the-loop treatment ``/api/hub/stats`` already has.
    """
    from . import system_stats
    from .hub_observability import StatSample

    def _sample_sync():
        return (
            system_stats.ram_stats(),
            system_stats.cpu_stats(),
            system_stats.gpu_stats(),
        )

    while True:
        try:
            ram, cpu, gpus = await asyncio.to_thread(_sample_sync)
            gpu0_vram = None
            gpu0_util = None
            if gpus:
                first = gpus[0]
                gpu0_vram = first.get("vram_percent")
                gpu0_util = first.get("util_percent")
            OBS.record_stat(
                StatSample(
                    ts=time.time(),
                    ram_percent=float(ram.get("percent", 0.0)),
                    cpu_percent=float(cpu.get("percent", 0.0)),
                    gpu0_vram_percent=gpu0_vram,
                    gpu0_util_percent=gpu0_util,
                )
            )
        except Exception:  # noqa: BLE001 — sampler must not die
            pass
        await asyncio.sleep(2.0)


def register(app) -> None:
    """Attach the hub's startup/shutdown handlers to ``app``.

    Calls ``app.on_event(event_type)`` as a plain function (its decorator
    return value) rather than using ``@app.on_event(...)`` sugar directly
    on these functions, so the handlers themselves stay plain module-level
    callables — importable and directly testable without needing a
    FastAPI app (``tests/test_restart_keepalive.py`` calls
    ``stop_backend_children()`` straight).
    """
    app.on_event("shutdown")(stop_background_tasks)
    app.on_event("shutdown")(stop_backend_children)
    app.on_event("startup")(wire_observatory_loop)
