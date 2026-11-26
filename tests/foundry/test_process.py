"""The process manager's foundry-local rows: command, environment and PID identity."""

from __future__ import annotations

import subprocess
import sys

import psutil
import pytest

from src.foundry import runtime
from src.model_registry import ENGINE_FOUNDRY, Model
from src.process import backend_process as bp
from tests.foundry._fake_sdk import ASR_VARIANT

ASR_ROW = Model(id="nemotron_asr", display_name="nemotron-3.5-asr", backend="whisper",
                engine=ENGINE_FOUNDRY, port=18090, runtime_model=ASR_VARIANT)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def _clean_states():
    bp._STATES.clear()
    yield
    bp._STATES.clear()


def _sleeper(*argv: str) -> subprocess.Popen:
    """A Python process whose cmdline carries ``argv`` after a sleeping ``-c``."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv],
                            creationflags=_NO_WINDOW)


@pytest.fixture
def worker_like():
    proc = _sleeper("-m", bp.FOUNDRY_WORKER_MODULE, ASR_ROW.id)
    yield proc
    proc.kill()
    proc.wait(timeout=5)


@pytest.fixture
def stranger():
    proc = _sleeper("-m", "http.server", "18090")
    yield proc
    proc.kill()
    proc.wait(timeout=5)


def test_command_runs_the_worker_module_for_the_row(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "sdk_version", lambda: runtime.SDK_PIN)
    from src import host_profile

    cmd = bp.build_command(ASR_ROW)
    assert cmd[1:] == ["-m", bp.FOUNDRY_WORKER_MODULE, ASR_ROW.id, "--config", str(host_profile.CONFIG_PATH)]


def test_command_without_the_sdk_says_foundry_local_is_not_installed(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "sdk_version", lambda: None)
    with pytest.raises(RuntimeError, match="Foundry Local not installed"):
        bp.build_command(ASR_ROW)


def test_spawned_worker_pid_is_the_process_that_runs_it() -> None:
    """The PID ``Popen`` returns must be the interpreter itself, inside the venv.

    A venv launcher would hand back its own PID while a child holds the
    port, so adoption, stop and identity checks would aim at the wrong one.
    """
    exe = bp._worker_python()[0]
    proc = subprocess.Popen(
        [exe, "-c", "import os, sys; print(os.getpid()); print(sys.prefix)"],
        env=bp.backend_env(ASR_ROW), stdout=subprocess.PIPE, text=True, creationflags=_NO_WINDOW,
    )
    out, _ = proc.communicate(timeout=60)
    printed_pid, prefix = out.split()
    assert int(printed_pid) == proc.pid
    assert prefix == sys.prefix


def test_worker_is_recognised_by_cmdline_not_exe(worker_like, stranger) -> None:
    assert bp.holder_confirmed(ASR_ROW, worker_like.pid) is True
    assert bp.holder_confirmed(ASR_ROW, stranger.pid) is False


def test_inherit_adopts_the_worker_and_records_its_identity(worker_like, monkeypatch) -> None:
    import src.process.server_process as sp

    monkeypatch.setattr(bp, "enabled_models", lambda: [ASR_ROW])
    monkeypatch.setattr(sp, "snapshot_listening_pids", lambda: {ASR_ROW.port: [worker_like.pid]})

    assert bp.inherit_running_backends() == 1
    state = bp._state_for(ASR_ROW.id)
    assert state.inherited_identity == bp._process_identity(psutil.Process(worker_like.pid))
    assert bp.pid(ASR_ROW.id) == worker_like.pid


def test_inherit_leaves_a_stranger_on_the_port_alone(stranger, monkeypatch) -> None:
    import src.process.server_process as sp

    monkeypatch.setattr(bp, "enabled_models", lambda: [ASR_ROW])
    monkeypatch.setattr(sp, "snapshot_listening_pids", lambda: {ASR_ROW.port: [stranger.pid]})

    assert bp.inherit_running_backends() == 0
    assert bp.is_running(ASR_ROW.id) is False


def test_foundry_asr_row_is_not_mistaken_for_whisper_cpp() -> None:
    assert not bp._is_whisper(ASR_ROW)
    assert bp._is_whisper(Model(id="w", display_name="w", backend="whisper"))
