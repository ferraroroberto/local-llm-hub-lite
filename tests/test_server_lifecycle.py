"""Unit tests for src/server_lifecycle.py's background-task lifecycle (#416).

``wire_observatory_loop`` spawns 3 perpetual/one-shot tasks on the running
loop (resource sampler, backend autostart, on-demand idle watchdog).
Before this fix nothing cancelled them on shutdown, so a torn-down ASGI
lifespan (every ``with TestClient(app) as c:`` exit in the test suite)
left them running against a dead loop — the order-dependent full-suite
pollution reported in #416.
"""

from __future__ import annotations

import asyncio

from src import backend_process, model_registry, server_lifecycle as sl


def _stub_real_ops(monkeypatch):
    """No real subprocess/network side effects from the lifecycle tasks."""
    monkeypatch.setattr(model_registry, "desired_model_ids", lambda *a, **kw: [])
    monkeypatch.setattr(backend_process, "inherit_running_backends", lambda: 0)


def test_wire_observatory_loop_tracks_its_tasks(monkeypatch):
    _stub_real_ops(monkeypatch)

    async def _run():
        await sl.wire_observatory_loop()
        assert len(sl._BACKGROUND_TASKS) == 3
        await sl.stop_background_tasks()

    asyncio.run(_run())
    assert sl._BACKGROUND_TASKS == []


def test_stop_background_tasks_cancels_the_perpetual_loops(monkeypatch):
    """Reproduces #416's leak: without cancellation, the perpetual loops
    (resource sampler, on-demand idle watchdog) are still pending when the
    lifespan tears down — proven here by asserting each task is both
    cancelled and finished after stop_background_tasks()."""
    _stub_real_ops(monkeypatch)

    async def _run():
        await sl.wire_observatory_loop()
        tasks = list(sl._BACKGROUND_TASKS)
        # The perpetual loops are still sleeping — not done.
        assert any(not t.done() for t in tasks)
        await sl.stop_background_tasks()
        for t in tasks:
            assert t.done()
            assert t.cancelled() or t.exception() is None

    asyncio.run(_run())


def test_stop_background_tasks_is_a_safe_noop_with_nothing_tracked():
    asyncio.run(sl.stop_background_tasks())
    assert sl._BACKGROUND_TASKS == []


def test_autostart_starts_the_desired_set(monkeypatch):
    """Autostart drives exactly the registry-derived desired set."""
    started: list[str] = []
    monkeypatch.setattr(model_registry, "desired_model_ids", lambda *a, **kw: ["qwen", "whisper"])
    monkeypatch.setattr(backend_process, "start", lambda mid: (started.append(mid), (True, "started"))[1])

    asyncio.run(sl._autostart_configured_backends())
    assert started == ["qwen", "whisper"]


def test_autostart_noop_with_empty_desired_set(monkeypatch):
    def _boom(mid):
        raise AssertionError("start must not be called")

    monkeypatch.setattr(model_registry, "desired_model_ids", lambda *a, **kw: [])
    monkeypatch.setattr(backend_process, "start", _boom)
    asyncio.run(sl._autostart_configured_backends())
