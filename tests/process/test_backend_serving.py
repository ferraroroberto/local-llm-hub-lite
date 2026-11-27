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


# What CPython 3.12.3 / 3.13.0 raise from ``os.kill(pid, CTRL_BREAK_EVENT)``
# when the hub has no console (a pythonw tray): the failed console event leaves
# an exception set, and the next builtin call reports it.
_PENDING_EXC = "<built-in function monotonic> returned a result with an exception set"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_stop_terminates_without_a_console_signal(monkeypatch, platform):
    """A stop never sends a console control event: the CPython bug above made
    it fail with ``error stopping: <built-in function monotonic> ...``."""
    monkeypatch.setattr(bp.sys, "platform", platform)
    calls: list = []

    class NoConsoleHub(FakePopen):
        def send_signal(self, sig: int) -> None:
            calls.append("signal")
            raise SystemError(_PENDING_EXC)

        def terminate(self) -> None:
            calls.append("terminate")
            self.returncode = 1

    state = _launch(NoConsoleHub())
    result = bp.stop(MODEL_ID)
    assert result.outcome is Outcome.STOPPED, result.message
    assert calls == ["terminate"]
    assert state.proc is None and state.launch is None


def test_a_failed_stop_keeps_the_launch_record(monkeypatch):
    """A process that outlives a failed stop is still ours: it must not read
    ``unknown`` ("process is alive but has no launch record")."""

    monkeypatch.setattr(bp.sys, "platform", "linux")

    class Unkillable(FakePopen):
        def terminate(self) -> None:
            raise OSError("access is denied")

    state = _launch(Unkillable())
    state.launch.ready = True
    result = bp.stop(MODEL_ID)
    assert result.outcome is Outcome.STOP_FAILED
    assert "access is denied" in result.message
    assert state.launch is not None

    verdict = bp.serving(_model(), reachable=False, listening={}, now=200.0)
    assert verdict.state is S.FAILED, verdict.reason


@pytest.mark.parametrize(
    "engine", ["llama-server", "whisper-server", "foundry-local"],
)
def test_every_backend_spawn_gets_its_own_group_and_no_window(monkeypatch, tmp_path, engine):
    """Each engine's child is spawned with ``CREATE_NEW_PROCESS_GROUP |
    CREATE_NO_WINDOW``, so nothing aimed at it reaches the hub's own group."""
    model = bp.Model(
        id="m", display_name="m", engine=engine, port=18999,
        backend="whisper" if engine != "llama-server" else "openai",
    )
    spawned: dict = {}

    class Recorder(FakePopen):
        def __init__(self, cmd, **kwargs) -> None:
            super().__init__()
            spawned.update(kwargs, cmd=cmd)

    monkeypatch.setattr(bp, "resolve_model_by_id", lambda mid: model)
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=0: False)
    monkeypatch.setattr(bp, "build_command", lambda m: ["backend.exe", m.engine])
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(bp, "checked_write_path", lambda p: p)
    monkeypatch.setattr(bp.subprocess, "Popen", Recorder)

    assert bp.start("m").outcome is Outcome.STARTED
    assert spawned["cmd"] == ["backend.exe", engine]
    assert spawned["creationflags"] == server_process.WIN_NEW_GROUP
