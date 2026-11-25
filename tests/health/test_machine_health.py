"""The machine health watch: verdicts, unknown readings, budgets and the endpoint."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.health import probes
from src.health.watch import HealthWatch

from ._fakes import FakeClock, Fixed, disk, healthy_probes, memory, ports

FIXTURE = Path(__file__).parent.parent / "fixtures" / "upstream_machine_health.json"


def _watch(clock: FakeClock, probe_map, **kw) -> HealthWatch:
    return HealthWatch(probe_map, clock=clock, host_id=lambda: "host-a", **kw)


def _finding(payload, metric):
    return next(f for f in payload["findings"] if f["metric"] == metric)


def test_no_sample_yet_is_unknown():
    clock = FakeClock()
    payload = _watch(clock, healthy_probes(clock)).current()
    assert payload["verdict"] == "unknown"
    assert payload["reason"] == "No sample yet"
    assert payload["sampled_at"] is None


def test_healthy_sample_is_ok_with_rows():
    clock = FakeClock()
    watch = _watch(clock, healthy_probes(clock))
    watch.tick()
    payload = watch.current()
    assert payload["verdict"] == "ok", payload["findings"]
    assert payload["system"]["commit_percent"] == 40.0
    assert payload["ports"]["tcp"]["percent"] == 5.0
    assert payload["ports"]["stale"] is False
    assert payload["disks"][0]["state"] == "ok"
    assert set(payload["coverage"].values()) == {"ok"}


def test_commit_over_act_line_is_act():
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "memory": Fixed(clock, memory(commit_percent=96.0))}
    watch = _watch(clock, probe_map)
    watch.tick()
    payload = watch.current()
    assert payload["verdict"] == "act"
    assert _finding(payload, "commit_percent")["level"] == "act"
    assert payload["alerts"][-1]["kind"] == "act"


def test_ports_over_watch_line_is_watch():
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "ports": Fixed(clock, ports(tcp_percent=60.0))}
    watch = _watch(clock, probe_map)
    watch.tick()
    assert _finding(watch.current(), "tcp_percent")["level"] == "watch"


def test_disk_load_needs_sustained_samples():
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "disk": Fixed(clock, disk(busy=99.0, queue=12.0, read_ms=250.0))}
    watch = _watch(clock, probe_map)
    states = []
    for _ in range(5):
        watch.tick()
        states.append(watch.current()["disks"][0]["state"])
        clock.advance(30)
    assert states == ["ok", "ok", "watch", "watch", "alert"]
    assert _finding(watch.current(), "disk_load")["level"] == "act"


@pytest.mark.parametrize("error, state, words", [
    (OSError("PDH status 0xC0000BB8"), "failed", "PDH status 0xC0000BB8"),
    (probes.Unsupported("not on this platform"), "unsupported", "not on this platform"),
    (probes.Warming("first window"), "warming", "first window"),
])
def test_a_reading_that_was_not_taken_is_unknown_with_its_reason(error, state, words):
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "disk": Fixed(clock, error)}
    watch = _watch(clock, probe_map)
    watch.tick()
    payload = watch.current()
    assert payload["verdict"] == "unknown"
    assert payload["coverage"]["disk"] == state
    finding = _finding(payload, "disk")
    assert finding["level"] == "unknown" and words in finding["reason"]
    assert payload["disks"] == []


def test_failed_memory_reading_leaves_no_commit_value():
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "memory": Fixed(clock, OSError("GetPerformanceInfo failed"))}
    watch = _watch(clock, probe_map)
    watch.tick()
    payload = watch.current()
    assert payload["verdict"] == "unknown"
    assert "commit_percent" not in payload["system"]
    assert "GetPerformanceInfo failed" in _finding(payload, "memory")["reason"]


def test_failed_ports_reading_marks_the_block_stale():
    clock = FakeClock()
    probe_map = {**healthy_probes(clock), "ports": Fixed(clock, OSError("access denied"))}
    watch = _watch(clock, probe_map)
    watch.tick()
    payload = watch.current()
    assert payload["ports"]["stale"] is True
    assert payload["ports"]["tcp"] is None and payload["ports"]["udp"] is None
    assert payload["coverage"]["tcp"] == "failed"


def test_slow_scanner_over_budget_is_unknown_never_its_value():
    clock = FakeClock()
    slow = Fixed(clock, ports(tcp_percent=1.0), cost_s=5.0)
    watch = _watch(clock, {**healthy_probes(clock), "ports": slow}, budget_ms=2000)
    watch.tick()
    payload = watch.current()
    assert payload["coverage"]["tcp"] == "overrun"
    assert payload["ports"]["tcp"] is None
    reason = _finding(payload, "ports")["reason"]
    assert "5000 ms" in reason and "2000 ms" in reason
    assert payload["verdict"] == "unknown"


def test_stale_sample_turns_unknown():
    clock = FakeClock()
    watch = _watch(clock, healthy_probes(clock), interval_s=30)
    watch.tick()
    clock.advance(30 * 3 + 1)
    payload = watch.current()
    assert payload["verdict"] == "unknown"
    assert "min old" in payload["reason"] or "s old" in payload["reason"]


def test_a_tick_still_running_is_named_once_stale():
    clock = FakeClock()
    watch = _watch(clock, healthy_probes(clock), interval_s=30)
    watch.tick()
    watch.mark_running()
    clock.advance(200)
    assert "still running" in watch.current()["reason"]


def test_sampler_spawns_no_subprocess(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError(f"sampler spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _refuse)
    watch = HealthWatch(probes.default_probes(), host_id=lambda: "host-a")
    for _ in range(2):
        watch.tick()
    payload = watch.current()
    assert payload["sampled_at"] is not None
    assert set(payload["coverage"]) == {"memory", "tcp", "disk"}


def _assert_same_shape(ours, theirs, path="body"):
    """Every key we serve is one upstream serves, with the same JSON type (a ``null``
    stands in for any type: an unmeasured reading)."""
    if isinstance(theirs, dict):
        assert isinstance(ours, dict), path
        extra = set(ours) - set(theirs)
        assert not extra, f"{path} has keys upstream lacks: {sorted(extra)}"
        for key in ours:
            if ours[key] is not None and theirs[key] is not None:
                _assert_same_shape(ours[key], theirs[key], f"{path}.{key}")
    elif isinstance(theirs, list):
        assert isinstance(ours, list), path
        if ours and theirs:
            _assert_same_shape(ours[0], theirs[0], f"{path}[0]")
    elif isinstance(theirs, bool) or isinstance(ours, bool):
        assert type(ours) is type(theirs), path
    elif isinstance(theirs, (int, float)):
        assert isinstance(ours, (int, float)), path
    else:
        assert type(ours) is type(theirs), path


def test_endpoint_has_upstream_shape(monkeypatch):
    from app_web.routers import machines
    from src.server import app

    clock = FakeClock()
    watch = _watch(clock, {**healthy_probes(clock), "memory": Fixed(clock, memory(commit_percent=88.0))})
    watch.tick()
    monkeypatch.setattr(machines.health_watch, "WATCH", watch)
    body = TestClient(app).get("/admin/api/machines/health").json()

    upstream = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(body) == set(upstream)
    _assert_same_shape(body, upstream)
    assert set(body["disks"][0]) == set(upstream["disks"][0])
    assert set(body["ports"]["top_holders"][0]) == set(upstream["ports"]["top_holders"][0])
    assert body["verdict"] == "watch"
    assert body["host_id"] == "host-a"
