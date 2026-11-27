"""An inherited backend PID is only trusted while it is still the same process."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from src.process import backend_process as bp
from src.process.outcomes import Outcome


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
    state.inherited_identity -= 100.0

    assert bp._inherited_alive(state) is False
    assert state.inherited_pid is None
    assert state.inherited_identity is None


_FRAMEWORK = "/Library/Frameworks/Python.framework/Versions/3.12"
_STUB_EXE = f"{_FRAMEWORK}/bin/python3.12"
_APP_EXE = f"{_FRAMEWORK}/Resources/Python.app/Contents/MacOS/Python"


@pytest.mark.parametrize(
    ("exe_at_adoption", "exe_later"),
    [
        pytest.param(_STUB_EXE, _APP_EXE, id="macos-framework-python-re-execs"),
        pytest.param("/usr/bin/python3.12", "/usr/bin/python3.12", id="exe-is-stable"),
    ],
)
def test_a_process_keeps_its_identity_when_its_exe_path_changes(
    exe_at_adoption, exe_later, monkeypatch
):
    """macOS framework Python replaces its own image just after spawn: same PID
    and start time, another exe path. A fake probe pins that on every platform."""
    exe = [exe_at_adoption]
    probe = SimpleNamespace(
        create_time=lambda: 1000.5, exe=lambda: exe[0], status=lambda: psutil.STATUS_RUNNING
    )
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(psutil, "Process", lambda pid: probe)
    state = _adopt("m", 4242)
    exe[0] = exe_later

    assert bp._inherited_alive(state) is True
    assert state.inherited_pid == 4242


def test_pid_without_recorded_identity_is_not_trusted(sleeper):
    state = bp._state_for("m")
    state.inherited_pid = sleeper.pid

    assert bp._inherited_alive(state) is False


def test_stop_never_kills_a_reused_pid(sleeper, monkeypatch):
    state = _adopt("m", sleeper.pid)
    state.inherited_identity = 0.0
    killed: list[int] = []
    monkeypatch.setattr(bp, "kill_pid", lambda pid: (killed.append(pid), (True, "killed"))[1])

    assert bp.stop("m").outcome is Outcome.NOT_RUNNING
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
