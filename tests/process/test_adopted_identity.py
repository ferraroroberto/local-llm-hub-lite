"""An adopted backend is identity-confirmed, start/stop outcomes are declared
values, and a hub never stops a backend it did not spawn.

Only stub listeners and stub processes are used: nothing here touches a real
llama-server port, and no real backend is started or stopped.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Iterator, Optional

import psutil
import pytest
from fastapi.testclient import TestClient

from src import model_registry, on_demand, server_lifecycle
from src.process import backend_process as bp
from src.process.outcomes import Outcome, Result
from src.process.server_process import snapshot_listening_pids
from src.process.serving_state import ServingState

MODEL_ID = "qwen35_4b"


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub_port() -> Iterator[int]:
    """A real listener on an ephemeral loopback port that answers 200 to any GET."""
    server = HTTPServer(("127.0.0.1", 0), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_states():
    bp._STATES.clear()
    yield
    bp._STATES.clear()


def _stub_model(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=MODEL_ID, backend="openai", engine="llama-server", port=port,
        url=f"http://127.0.0.1:{port}/v1", model_path="models/x.gguf", args=[],
        virtual=False,
    )


# --- 1. an unrelated listener is never adopted -------------------------------


def test_an_unrelated_listener_reads_unknown_not_adopted(stub_port):
    model = _stub_model(stub_port)
    listening = snapshot_listening_pids()
    assert stub_port in listening, "the stub listener must be visible to the port snapshot"

    got = bp.serving(model, True, listening)  # type: ignore[arg-type]

    assert got.state is ServingState.UNKNOWN
    assert got.reason is not None and "not this model's server binary" in got.reason
    assert got.ownership == "none"


def test_a_listener_whose_identity_cannot_be_read_reads_unknown(stub_port, monkeypatch):
    model = _stub_model(stub_port)
    monkeypatch.setattr(psutil, "Process", lambda pid: (_ for _ in ()).throw(psutil.AccessDenied()))

    got = bp.serving(model, True, snapshot_listening_pids())  # type: ignore[arg-type]

    assert got.state is ServingState.UNKNOWN
    assert got.reason is not None and "could not be confirmed" in got.reason


def test_start_refuses_to_adopt_an_unrelated_listener(stub_port, monkeypatch):
    model = _stub_model(stub_port)
    monkeypatch.setattr(bp, "resolve_model_by_id", lambda mid: model)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("start must not spawn over a listener it cannot identify"),
    )

    result = bp.start(MODEL_ID)

    assert result.outcome is Outcome.UNCONFIRMED_LISTENER
    assert result.ok is False
    assert bp._state_for(MODEL_ID).launch is None


def test_start_adopts_a_confirmed_backend(stub_port, monkeypatch):
    model = _stub_model(stub_port)
    monkeypatch.setattr(bp, "resolve_model_by_id", lambda mid: model)
    monkeypatch.setattr(bp, "holder_confirmed", lambda m, pid: True)

    result = bp.start(MODEL_ID)

    assert result.outcome is Outcome.ADOPTED
    assert result.ok is True


def test_the_binary_check_matches_the_engine_not_just_any_exe():
    llama = SimpleNamespace(engine="llama-server", backend="openai")
    whisper = SimpleNamespace(engine="whisper-server", backend="whisper")
    assert bp._looks_like_backend_binary("c:/x/llama-server.exe", llama)
    assert not bp._looks_like_backend_binary("c:/x/python.exe", llama)
    assert not bp._looks_like_backend_binary("c:/x/llama-server.exe", whisper)


# --- 2. start / stop outcomes are declared values ----------------------------


def test_a_failure_whose_text_says_already_running_is_still_a_failure(monkeypatch):
    """Callers read ``outcome``; a message that happens to contain the old
    magic words changes nothing."""
    model = SimpleNamespace(
        id="m", startup="on_demand", virtual=False, port=9, url="http://127.0.0.1:9/v1",
        backend="openai", engine="llama-server", est_vram_mb=0, idle_unload_minutes=None,
    )
    monkeypatch.setattr(on_demand, "is_on_demand", lambda m: True)
    monkeypatch.setattr(on_demand, "_warn_on_vram_overcommit", lambda m: None)
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: False)
    monkeypatch.setattr(
        bp, "start", lambda mid: Result(Outcome.NOT_LAUNCHABLE, "already running (not really)")
    )

    with pytest.raises(on_demand.OnDemandNotReady, match="start of 'm' failed"):
        on_demand.ensure_ready(model)  # type: ignore[arg-type]


def test_already_running_is_read_from_the_outcome_and_keeps_polling(monkeypatch):
    model = SimpleNamespace(
        id="m", startup="on_demand", virtual=False, port=9, url="http://127.0.0.1:9/v1",
        backend="openai", engine="llama-server", est_vram_mb=0, idle_unload_minutes=None,
    )
    answers = iter([False, False, False, True])
    monkeypatch.setattr(on_demand, "is_on_demand", lambda m: True)
    monkeypatch.setattr(on_demand, "_warn_on_vram_overcommit", lambda m: None)
    monkeypatch.setattr(on_demand, "READY_POLL_S", 0.0)
    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.5: next(answers))
    monkeypatch.setattr(bp, "start", lambda mid: Result(Outcome.ALREADY_RUNNING))
    monkeypatch.setattr(
        bp, "serving", lambda *a, **k: SimpleNamespace(state=ServingState.LOADING, reason=None)
    )

    on_demand.ensure_ready(model)  # type: ignore[arg-type]


def test_result_message_is_derived_from_the_outcome():
    assert Result(Outcome.ALREADY_RUNNING).message == "already running"
    assert Result(Outcome.NOT_RUNNING).message == "not running"
    assert Result(Outcome.STARTED, pid=7).message == "started (pid=7)"
    assert Result(Outcome.ADOPTED, pid=7).message == "adopted external instance (PID 7)"
    assert {Outcome.STARTED, Outcome.ADOPTED, Outcome.STOPPED} == {o for o in Outcome if Result(o).ok}


def test_stop_declares_not_running_and_stopped(monkeypatch):
    assert bp.stop(MODEL_ID).outcome is Outcome.NOT_RUNNING

    class _Proc:
        pid = 1
        returncode: Optional[int] = None

        def poll(self) -> Optional[int]:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float = 0) -> int:
            return 0

        def send_signal(self, sig: int) -> None:
            pass

    bp._state_for(MODEL_ID).proc = _Proc()  # type: ignore[assignment]
    assert bp.stop(MODEL_ID).outcome is Outcome.STOPPED


def _admin_client() -> TestClient:
    from app_web.server import create_app

    return TestClient(create_app())


@pytest.mark.parametrize(
    ("route", "attr", "result", "status", "outcome"),
    [
        ("start", "start", Result(Outcome.STARTED, pid=3), 200, "started"),
        ("start", "start", Result(Outcome.ALREADY_RUNNING), 409, "already_running"),
        ("stop", "stop", Result(Outcome.STOPPED), 200, "stopped"),
        ("stop", "stop", Result(Outcome.NOT_RUNNING), 409, "not_running"),
    ],
)
def test_the_admin_route_carries_the_declared_outcome(
    monkeypatch, route, attr, result, status, outcome
):
    monkeypatch.setattr(bp, attr, lambda mid: result)

    resp = _admin_client().post(f"/api/models/{MODEL_ID}/{route}")

    assert resp.status_code == status
    body = resp.json()
    assert body["outcome"] == outcome
    assert body["ok"] is (status == 200)
    assert body["detail"] == result.message


# --- 3. shutdown never stops a backend the hub did not spawn -----------------


@pytest.fixture
def stub_backend() -> Iterator[subprocess.Popen]:
    """A stand-in for a backend another process started (it never gets killed by the hub)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    yield proc
    proc.kill()
    proc.wait(timeout=5)


def _run(coro) -> None:
    box: dict = {}

    def _worker() -> None:
        try:
            import asyncio

            asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]


def test_shutdown_leaves_an_adopted_backend_running_and_stops_a_spawned_one(
    stub_backend, monkeypatch
):
    enabled = {m.id for m in model_registry.enabled_models()}
    assert {"qwen35_4b", "whisper"} <= enabled

    # whisper: inherited from another hub (adopted, not started by this one).
    adopted = bp._state_for("whisper")
    adopted.inherited_pid = stub_backend.pid
    adopted.inherited_identity = bp._process_identity(psutil.Process(stub_backend.pid))

    # qwen: spawned by this hub.
    spawned = SimpleNamespace(terminated=False)

    class _Spawned:
        pid = 4242

        def poll(self) -> Optional[int]:
            return 0 if spawned.terminated else None

        def terminate(self) -> None:
            spawned.terminated = True

        def wait(self, timeout: float = 0) -> int:
            return 0

        def send_signal(self, sig: int) -> None:
            pass

    bp._state_for("qwen35_4b").proc = _Spawned()  # type: ignore[assignment]
    killed: list[int] = []
    monkeypatch.setattr(bp, "kill_pid", lambda pid: (killed.append(pid), (True, "killed"))[1])
    assert bp.is_running("whisper") is True

    _run(server_lifecycle.stop_backend_children())

    assert spawned.terminated is True, "a backend the hub spawned is stopped on shutdown"
    assert killed == [], "an adopted backend is never killed on shutdown"
    assert stub_backend.poll() is None, "the adopted backend process is still alive"
    assert bp.is_running("whisper") is True


def test_spawned_backends_excludes_an_inherited_one(stub_backend):
    state = bp._state_for("whisper")
    state.inherited_pid = stub_backend.pid
    state.inherited_identity = bp._process_identity(psutil.Process(stub_backend.pid))

    assert "whisper" in bp.running_backends()
    assert "whisper" not in bp.spawned_backends()

