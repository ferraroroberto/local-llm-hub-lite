"""Named pools and the admin request-timing middleware.

Blocking admin work runs on a pool named for its class, and the middleware
reports which pool a request used. No GPU, backend or network is involved:
the calls under test are stand-ins.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app_web import server as admin_server
from src import request_pools
from src import server as server_mod
from src.request_timing import TimedExecutor

_MARK: contextvars.ContextVar[str] = contextvars.ContextVar("mark", default="unset")


def _thread_name() -> str:
    return threading.current_thread().name


def test_run_in_pool_runs_on_the_named_pool():
    name = asyncio.run(request_pools.run_in_pool(request_pools.HOT_POLL, _thread_name))
    assert name.startswith("hot-poll")


def test_each_class_of_work_has_its_own_pool():
    pools = [
        request_pools.HOT_POLL,
        request_pools.HEAVY_READ,
        request_pools.USER_ACTION,
        request_pools.BACKEND_LIFECYCLE,
        request_pools.BACKEND_READY,
        request_pools.SAMPLER,
    ]
    assert len({p.pool_name for p in pools}) == len(pools)
    assert all(isinstance(p, TimedExecutor) for p in pools)


def test_run_in_pool_carries_the_callers_context():
    async def go() -> str:
        _MARK.set("from-caller")
        return await request_pools.run_in_pool(request_pools.USER_ACTION, _MARK.get)

    assert asyncio.run(go()) == "from-caller"


def test_run_in_pool_passes_args_and_kwargs():
    got = asyncio.run(
        request_pools.run_in_pool(request_pools.HOT_POLL, lambda a, b=0: (a, b), 1, b=2)
    )
    assert got == (1, 2)


def test_run_in_pool_raises_what_the_call_raised():
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        asyncio.run(request_pools.run_in_pool(request_pools.HEAVY_READ, boom))


def test_a_busy_pool_does_not_hold_up_another():
    release = threading.Event()

    async def go() -> str:
        slow = [
            asyncio.ensure_future(request_pools.run_in_pool(request_pools.HEAVY_READ, release.wait, 5))
            for _ in range(2)
        ]
        try:
            # Both heavy-read workers are held; a hot-poll call still answers.
            return await asyncio.wait_for(
                request_pools.run_in_pool(request_pools.HOT_POLL, _thread_name), timeout=2
            )
        finally:
            release.set()
            await asyncio.gather(*slow)

    assert asyncio.run(go()).startswith("hot-poll")


def _loopback(app: FastAPI) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def test_admin_api_reports_the_pool_a_request_used(monkeypatch):
    from src import system_stats

    monkeypatch.setattr(system_stats, "gpu_stats", lambda: [])

    resp = _loopback(server_mod.app).get("/admin/api/hub/stats")

    assert resp.status_code == 200
    timing = resp.headers["server-timing"]
    assert 'desc="hot-poll"' in timing
    assert "app;dur=" in timing


def test_admin_static_files_carry_no_server_timing():
    resp = _loopback(server_mod.app).get("/admin/static/styles.css")

    assert resp.status_code == 200
    assert "server-timing" not in resp.headers


def test_event_streams_and_playground_calls_are_not_timed():
    app = FastAPI()
    app.add_middleware(admin_server._AdminTimingMiddleware, timed_prefix="/admin/api/")

    @app.get("/admin/api/hub/log/tail")
    async def tail() -> dict:
        return {}

    @app.post("/admin/api/playground/send")
    async def send() -> dict:
        return {}

    @app.get("/admin/api/hub/stats")
    async def stats() -> dict:
        return {}

    client = TestClient(app)
    assert "server-timing" not in client.get("/admin/api/hub/log/tail").headers
    assert "server-timing" not in client.post("/admin/api/playground/send").headers
    assert "server-timing" in client.get("/admin/api/hub/stats").headers


def test_install_checks_run_on_the_heavy_read_pool(monkeypatch):
    seen: list[str] = []

    def fake_checks(use_cache: bool = False):
        seen.append(_thread_name())
        return type("R", (), {"worst_status": "ok", "ok": True, "checks": []})()

    from src import install

    monkeypatch.setattr(install, "run_all_checks", fake_checks)

    resp = _loopback(server_mod.app).get("/admin/api/install/status")

    assert resp.status_code == 200
    assert seen and seen[0].startswith("heavy-read")
