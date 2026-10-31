"""An inherited backend PID is only trusted while it is still the same process."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from src.process import backend_process as bp


@pytest.fixture
def sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    yield proc
    proc.kill()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _clean_states():
    bp._STATES.clear()
    yield
    bp._STATES.clear()


def _adopt(model_id: str, pid: int) -> bp._BackendState:
    state = bp._state_for(model_id)
    state.inherited_pid = pid
    state.inherited_identity = bp._process_identity(psutil.Process(pid))
    return state


def test_same_process_stays_alive(sleeper):
    state = _adopt("m", sleeper.pid)

    assert bp._inherited_alive(state) is True
    assert state.inherited_pid == sleeper.pid


def test_reused_pid_with_new_create_time_is_dropped(sleeper):
    state = _adopt("m", sleeper.pid)
    create_time, exe = state.inherited_identity
    state.inherited_identity = (create_time - 100.0, exe)

    assert bp._inherited_alive(state) is False
    assert state.inherited_pid is None
    assert state.inherited_identity is None


def test_reused_pid_with_other_exe_is_dropped(sleeper):
    state = _adopt("m", sleeper.pid)
    state.inherited_identity = (state.inherited_identity[0], "c:/other/llama-server.exe")

    assert bp._inherited_alive(state) is False


def test_pid_without_recorded_identity_is_not_trusted(sleeper):
    state = bp._state_for("m")
    state.inherited_pid = sleeper.pid

    assert bp._inherited_alive(state) is False


def test_stop_never_kills_a_reused_pid(sleeper, monkeypatch):
    state = _adopt("m", sleeper.pid)
    state.inherited_identity = (0.0, "")
    killed: list[int] = []
    monkeypatch.setattr(bp, "kill_pid", lambda pid: (killed.append(pid), (True, "killed"))[1])

    assert bp.stop("m") == (False, "not running")
    assert killed == []
    assert bp.is_running("m") is False


def test_inherit_records_identity_so_the_adopted_backend_stays_running(sleeper, monkeypatch):
    model = SimpleNamespace(
        id="m", backend="openai", port=8123, engine="llama-server", url=None
    )
    monkeypatch.setattr(bp, "enabled_models", lambda: [model])
    monkeypatch.setattr(bp, "_looks_like_backend_binary", lambda exe, m: True)
    import src.process.server_process as sp

    monkeypatch.setattr(sp, "snapshot_listening_pids", lambda: {8123: [sleeper.pid]})

    assert bp.inherit_running_backends() == 1
    assert bp.is_running("m") is True
    assert bp._state_for("m").inherited_identity is not None
