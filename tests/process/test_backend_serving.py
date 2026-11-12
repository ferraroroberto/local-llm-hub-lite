"""``backend_process.serving``: the launch record and the port snapshot feed
``derive``. No process is spawned; Popen handles are tiny fakes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import psutil
import pytest

from src.process import backend_process as bp
from src.process import server_process
from src.process.outcomes import Outcome
from src.process.serving_state import LaunchRecord, ServingState

S = ServingState
MODEL_ID = "qwen35_4b"


class FakePopen:
    pid = 4242

    def __init__(self, returncode: Optional[int] = None) -> None:
        self.returncode = returncode

    def poll(self) -> Optional[int]:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float = 0) -> int:
        return self.returncode or 0

    def send_signal(self, sig: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_states():
    bp._STATES.clear()
    yield
    bp._STATES.clear()


def _model():
    model = bp.resolve_model_by_id(MODEL_ID)
    assert model is not None
    return model


def _launch(proc: FakePopen, spawned_at: float = 100.0) -> bp._BackendState:
    state = bp._state_for(MODEL_ID)
    state.proc = proc
    state.launch = LaunchRecord(spawned_at=spawned_at)
    return state


def test_a_process_that_exits_while_loading_reads_failed_with_its_exit_code():
    # state-machine-proof: serving
    _launch(FakePopen(returncode=3))
    got = bp.serving(_model(), False, {}, now=101.0)
    assert got.state is S.FAILED
    assert got.reason == "process exited during load (exit code 3)"


def test_a_deliberate_stop_reads_stopped_not_failed():
    # state-machine-proof: serving
    _launch(FakePopen(returncode=3))
    assert bp.stop(MODEL_ID).outcome is Outcome.NOT_RUNNING
    got = bp.serving(_model(), False, {}, now=101.0)
    assert got.state is S.STOPPED


def test_a_live_process_loads_then_latches_ready_then_fails_when_it_stops_answering():
    # state-machine-proof: serving
    state = _launch(FakePopen())
    model = _model()
    assert bp.serving(model, False, {}, now=110.0).state is S.LOADING
    assert bp.serving(model, True, {model.port: [4242]}, now=111.0).state is S.READY
    assert state.launch is not None and state.launch.ready
    got = bp.serving(model, False, {model.port: [4242]}, now=112.0)
    assert got.state is S.FAILED
    assert got.reason == "process is alive but stopped answering"


def test_a_live_process_that_never_answers_fails_at_the_deadline():
    _launch(FakePopen(), spawned_at=0.0)
    got = bp.serving(_model(), False, {}, now=181.0)
    assert got.state is S.FAILED
    assert got.reason == "did not answer within 180s of starting"


def test_a_failed_port_lookup_is_unknown_not_stopped():
    # state-machine-proof: serving
    failed = server_process.ListeningSnapshot()
    failed.known = False
    assert bp.serving(_model(), False, failed).state is S.UNKNOWN
    assert bp.serving(_model(), False, None).state is S.UNKNOWN
    assert bp.serving(_model(), False, {}).state is S.STOPPED


def test_a_confirmed_foreign_listener_reads_adopted_with_its_pid(monkeypatch):
    monkeypatch.setattr(bp, "holder_confirmed", lambda model, pid: True)
    got = bp.serving(_model(), True, {_model().port: [777]})
    assert got.state is S.ADOPTED
    assert (got.ownership, got.pid) == ("external", 777)


def test_start_writes_a_launch_record(monkeypatch, tmp_path: Path):
    log = tmp_path / "backend.log"
    monkeypatch.setattr(bp, "_roll_log", lambda model_id: log)
    monkeypatch.setattr(bp, "checked_write_path", lambda p: p)
    monkeypatch.setattr(bp, "build_command", lambda m: ["fake-server"])
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakePopen())

    assert bp.start(MODEL_ID).outcome is Outcome.STARTED
    launch = bp._state_for(MODEL_ID).launch
    assert launch is not None and not launch.ready
    assert bp.stop(MODEL_ID).outcome is Outcome.STOPPED
    assert bp._state_for(MODEL_ID).launch is None


def test_listening_snapshot_says_unknown_when_every_lookup_fails(monkeypatch):
    def _denied(*a, **kw):
        raise psutil.AccessDenied()

    def _missing(*a, **kw):
        raise FileNotFoundError("netstat")

    monkeypatch.setattr(psutil, "net_connections", _denied)
    monkeypatch.setattr(server_process.subprocess, "run", _missing)

    snapshot = server_process.snapshot_listening_pids()
    assert dict(snapshot) == {}
    assert server_process.snapshot_is_known(snapshot) is False
    assert server_process.snapshot_is_known({}) is True


def test_stop_gives_ctrl_break_a_grace_wait_before_terminating(monkeypatch):
    monkeypatch.setattr(bp.sys, "platform", "win32")
    monkeypatch.setattr(bp.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    calls: list = []

    class Polite(FakePopen):
        def send_signal(self, sig: int) -> None:
            calls.append("signal")

        def wait(self, timeout: float = 0) -> int:
            calls.append(("wait", timeout))
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            calls.append("terminate")

    _launch(Polite())
    assert bp.stop(MODEL_ID).outcome is Outcome.STOPPED
    assert calls == ["signal", ("wait", bp.STOP_GRACE_S)]
