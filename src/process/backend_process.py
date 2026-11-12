"""Per-model backend process manager.

Each enabled local model in the registry gets its own singleton process +
per-backend log file (``data/logs/backend-<id>.log``) here. Keyed by model
id ("qwen35_4b", "whisper"). Used by the admin SPA's Models tab, the
tray, and the per-model launcher scripts to start/stop individual backends
and tail their output without global-state entanglement. The log file is
written by the child (not a hub-owned pipe), so it stays readable across a
hub restart and an inherited backend never writes into a closed pipe.

Two engine families share this manager:
  - `llama-server` for chat/completion GGUF models
  - `whisper-server` for whisper.cpp ASR (OpenAI-compatible /v1/audio/*)
The shape differences (binary location, -m vs --model flag, health
endpoint) are absorbed in `build_command` and `is_reachable`.

``start(model_id)`` adopts an already-reachable backend on the model's
port instead of spawning a duplicate, but only once the listener is
confirmed to be that engine's binary; ``stop(model_id)`` only stops what
we spawned or inherited. Both return a :class:`~.outcomes.Result` whose
``outcome`` is the declared value callers branch on. Use
:func:`force_stop_external` to reclaim a port held by someone else.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Mapping, Optional

from ..http_client import get_sync_client
from ..model_registry import Model, enabled_models
from ..runtime_files import checked_write_path
from .outcomes import Outcome, Result
from .server_process import (
    WIN_NEW_GROUP,
    kill_pid,
    resolve_external_pid,
    snapshot_is_known,
)
from .serving_state import LaunchRecord, Observation, Serving, ServingState, derive

if TYPE_CHECKING:
    import psutil

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENDOR_LLAMA = PROJECT_ROOT / "vendor" / "llama.cpp"
VENDOR_WHISPER = PROJECT_ROOT / "vendor" / "whisper.cpp"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
# Tail size returned by ``log_lines``.
LOG_TAIL_LINES = 400
# Listen address passed to every backend. The hub is the only intended
# client and reaches each backend over loopback (``Model.url``); other
# machines go through the hub on :8000. A row's own ``args`` come later on
# the command line, so ``--host`` there still overrides this.
BACKEND_LISTEN_HOST = "127.0.0.1"
# ``stop()`` timings. After CTRL_BREAK the child gets this long to exit on its
# own, so a backend that handles the signal shuts down cleanly before
# TerminateProcess is used.
STOP_GRACE_S = 3.0
# Wait for the process to disappear after TerminateProcess; a healthy exit is
# near-instant, so running past this means it is stuck and is killed outright.
STOP_TERMINATE_WAIT_S = 8.0
# Final wait after kill(); the process is gone unless the OS holds it in an
# uninterruptible state, which this bounds so ``stop()`` always returns.
STOP_KILL_WAIT_S = 5.0


def _log_path(model_id: str) -> Path:
    """Per-backend log file: ``data/logs/backend-<id>.log`` (child-owned)."""
    return LOG_DIR / f"backend-{model_id}.log"


def _roll_log(model_id: str) -> Path:
    """Roll the previous run's log to ``.log.1`` and return the fresh path.

    Bounds growth to two files per backend (current + one backup) without
    needing the hub to manage rotation mid-run: the child owns the fd, so
    we can only rotate at spawn time. Best-effort — a failed roll never
    blocks a launch.
    """
    path = _log_path(model_id)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            path.replace(backup)
    except OSError:
        pass
    return path


def llama_server_binary() -> Path:
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    return VENDOR_LLAMA / name


def whisper_server_binary() -> Path:
    name = "whisper-server.exe" if sys.platform == "win32" else "whisper-server"
    return VENDOR_WHISPER / name


def _is_whisper(model: Model) -> bool:
    return model.engine == "whisper-server" or model.backend == "whisper"


def vendor_dir_for(model: Model) -> Path:
    return VENDOR_WHISPER if _is_whisper(model) else VENDOR_LLAMA


class _BackendState:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        # PID of a backend the hub inherited at startup — a process the
        # hub didn't itself spawn but recognises as one of its model
        # binaries listening on the right port. ``stop`` taskkill's it
        # instead of calling ``proc.terminate``. Its stdout still lands in
        # the per-backend log file (the child owns that fd), so ``log_lines``
        # returns its tail across a hub restart — see ``_log_path``.
        self.inherited_pid: Optional[int] = None
        # (create_time, exe) of ``inherited_pid`` captured at adoption. A PID
        # alone is not an identity: once the backend dies the OS may hand the
        # number to an unrelated process, which ``stop`` would then kill.
        self.inherited_identity: Optional[tuple[float, str]] = None
        # The current spawn (or inheritance), set by ``start`` /
        # ``inherit_running_backends`` and dropped only by a deliberate
        # ``stop``. A record whose process is gone is how "died on its own"
        # reads differently from "stopped" (see ``serving``).
        self.launch: Optional[LaunchRecord] = None

    def forget_inherited(self) -> None:
        self.inherited_pid = None
        self.inherited_identity = None


_STATES: Dict[str, _BackendState] = {}

def _state_for(model_id: str) -> _BackendState:
    state = _STATES.get(model_id)
    if state is None:
        state = _BackendState()
        _STATES[model_id] = state
    return state


def spawned_backends() -> Dict[str, Model]:
    """Backends this hub spawned itself and still owns, keyed by model id.

    Narrower than :func:`running_backends`: an inherited backend is excluded.
    Another hub started it, so this hub's shutdown must leave it running.
    """
    out: Dict[str, Model] = {}
    for m in enabled_models():
        p = _state_for(m.id).proc
        if m.backend in ("openai", "whisper") and p is not None and p.poll() is None:
            out[m.id] = m
    return out


def is_running(model_id: str) -> bool:
    state = _state_for(model_id)
    p = state.proc
    if p is not None and p.poll() is None:
        return True
    return _inherited_alive(state)


def pid(model_id: str) -> Optional[int]:
    state = _state_for(model_id)
    p = state.proc
    if p is not None and p.poll() is None:
        return p.pid
    if _inherited_alive(state):
        return state.inherited_pid
    return None


def _process_identity(proc: "psutil.Process") -> tuple[float, str]:
    """Create-time and lower-cased exe path: what makes a PID this process."""
    return (proc.create_time(), (proc.exe() or "").lower())


def _inherited_alive(state: "_BackendState") -> bool:
    pid_ = state.inherited_pid
    if pid_ is None:
        return False
    try:
        import psutil

        if not psutil.pid_exists(pid_):
            state.forget_inherited()
            return False
        proc = psutil.Process(pid_)
        if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            state.forget_inherited()
            return False
        # PID reuse on Windows is aggressive: the adopted backend may have
        # died and its number been given to another process. Anything but the
        # identity recorded at adoption is stale — fail closed, since a false
        # "alive" lets ``stop`` kill an unrelated process.
        if _process_identity(proc) != state.inherited_identity:
            logger.info("ℹ️ inherited PID %s no longer the adopted backend; dropping it", pid_)
            state.forget_inherited()
            return False
        return True
    except Exception:  # noqa: BLE001
        state.forget_inherited()
        return False


def is_reachable(model: Model, timeout: float = 1.5) -> bool:
    if not model.url:
        return False
    # NB: strip the literal "/v1" suffix, not a character set. `str.rstrip`
    # takes a set of chars, so `"...:8071/v1".rstrip("/v1")` eats the port's
    # trailing "1" too and yields ":807" — a dead port. removesuffix is exact.
    base = model.url.removesuffix("/v1").rstrip("/")
    if model.engine == "whisper-server":
        # whisper.cpp server has no /health; GET / returns 200 once loaded.
        try:
            r = get_sync_client().get(f"{base}/", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False
    try:
        r = get_sync_client().get(f"{base}/health", timeout=timeout)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    # llama-server /v1/models is always available once loaded
    try:
        r = get_sync_client().get(f"{model.url}/models", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def holder_confirmed(model: Model, holder_pid: Optional[int]) -> Optional[bool]:
    """Is the process holding *model*'s port that engine's binary?

    ``True`` confirmed, ``False`` it is another program, ``None`` it could not
    be established (no PID, the process is gone, or its exe is unreadable). A
    listener only counts as the backend once this is ``True``.
    """
    if holder_pid is None:
        return None
    try:
        import psutil

        exe = psutil.Process(holder_pid).exe()
    except Exception:  # noqa: BLE001 — NoSuchProcess / AccessDenied / OSError all mean "unreadable"
        return None
    if not exe:
        return None
    return _looks_like_backend_binary(exe, model)


def serving(
    model: Model,
    reachable: Optional[bool],
    listening: Optional[Mapping[int, list]],
    now: Optional[float] = None,
) -> Serving:
    """The model's serving state: the one derivation every reader uses.

    ``reachable`` is the caller's probe of the backend (:func:`is_reachable`),
    ``None`` when it could not be run. ``listening`` is the port → PIDs
    snapshot, ``None`` when it was not gathered or its lookup failed (a
    ``ListeningSnapshot`` with ``known = False`` counts as failed). A reader
    that only wants the process verdict (the on-demand loader) passes
    ``None`` and ignores the port facts. Latches the launch record's
    ``ready`` flag the first time a live process answers.
    """
    state = _state_for(model.id)
    if listening is not None and not snapshot_is_known(listening):
        listening = None
    t = time.monotonic() if now is None else now
    alive = is_running(model.id)
    holders = listening.get(model.port) if listening is not None and model.port else None
    exit_code: Optional[int] = None
    if alive:
        pid_ = pid(model.id)
    else:
        pid_ = holders[0] if holders else None
        if state.proc is not None:
            exit_code = state.proc.poll()
    launch = state.launch
    obs = Observation(
        alive=alive,
        exit_code=exit_code,
        launched=launch is not None,
        was_ready=bool(launch and launch.ready),
        load_age_s=None if launch is None else t - launch.spawned_at,
        probe=reachable,
        port_held=None if listening is None and model.port else bool(holders),
        pid=pid_,
        holder_confirmed=None if alive or not holders else holder_confirmed(model, pid_),
    )
    result = derive(obs)
    if result.state is ServingState.READY and launch is not None:
        launch.ready = True
    return result


def log_lines(model_id: str, limit: int = LOG_TAIL_LINES) -> list[str]:
    """Tail of the backend's log file (``data/logs/backend-<id>.log``).

    Reads the file the child writes its stdout/stderr to, so it works for
    a backend we spawned *and* one we inherited across a hub restart (the
    child owns the fd). Returns ``[]`` if the backend has never started.
    """
    path = _log_path(model_id)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return lines[-limit:] if limit else lines


def build_command(model: Model) -> list[str]:
    if not model.model_path:
        raise RuntimeError(f"model {model.id} has no model_path")
    model_path = (PROJECT_ROOT / model.model_path).resolve()

    if _is_whisper(model):
        bin_path = whisper_server_binary()
        if not bin_path.exists():
            raise RuntimeError(
                f"whisper-server not found at {bin_path} - run scripts/install_whisper_cpp.py"
            )
        if not model_path.exists():
            raise RuntimeError(
                f"whisper model not found at {model_path} - run scripts/download_models.py --only {model.id}"
            )
        cmd = [
            str(bin_path),
            "--host", BACKEND_LISTEN_HOST,
            "--port", str(model.port),
            "--model", str(model_path),
        ]
        cmd.extend(model.args or [])
        return cmd

    bin_path = llama_server_binary()
    if not bin_path.exists():
        raise RuntimeError(f"llama-server not found at {bin_path} - run scripts/install_llama_cpp.py")
    if not model_path.exists():
        raise RuntimeError(f"GGUF not found at {model_path} - run scripts/download_models.py --only {model.id}")
    cmd = [
        str(bin_path),
        "-m", str(model_path),
        "--host", BACKEND_LISTEN_HOST,
        "--port", str(model.port),
    ]
    cmd.extend(model.args or [])
    return cmd


def start(model_id: str) -> Result:
    model = resolve_model_by_id(model_id)
    if model is None:
        return Result(Outcome.UNKNOWN_MODEL, model_id)
    if is_running(model_id):
        return Result(Outcome.ALREADY_RUNNING)

    # Adopt an external instance already listening on this model's port, but
    # only once it is confirmed to be this engine's binary: a stranger that
    # answers on the port is not a backend we may route to as ours.
    if is_reachable(model, timeout=0.4):
        ext = external_pid(model_id)
        if holder_confirmed(model, ext) is not True:
            who = f"PID {ext}" if ext else "an unidentified process"
            logger.warning(
                "⚠️ %s: :%s answers but its holder (%s) is not confirmed as the model's server; not adopting",
                model_id, model.port, who,
            )
            return Result(
                Outcome.UNCONFIRMED_LISTENER,
                f"port {model.port} answers but its holder ({who}) is not confirmed "
                f"as this model's server binary",
                ext,
            )
        return Result(Outcome.ADOPTED, pid=ext)

    state = _state_for(model_id)

    try:
        cmd = build_command(model)
    except Exception as e:
        return Result(Outcome.NOT_LAUNCHABLE, str(e))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Help the server find the cudart DLLs shipped next to its binary.
    if sys.platform == "win32":
        env["PATH"] = str(vendor_dir_for(model)) + os.pathsep + env.get("PATH", "")

    # Redirect stdout/stderr to a child-owned log file instead of a hub-owned
    # pipe. The child keeps its own fd, so the log survives a hub restart and
    # an inherited backend never writes into a closed pipe (the class of
    # [Errno 22] this avoids). The file is also readable on disk and
    # via ``log_lines`` / the admin log endpoint. Roll first for a fresh log.
    log_file = checked_write_path(_roll_log(model_id)).open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=WIN_NEW_GROUP,
        )
    except Exception as e:
        return Result(Outcome.LAUNCH_FAILED, f"failed to launch: {e}")
    finally:
        # The child duplicated the handle at spawn; the hub no longer needs
        # its own copy (and keeping it open would pin the file). Closing it is
        # exactly what makes the log restart-safe — only the child holds the fd.
        log_file.close()

    state.proc = proc
    state.launch = LaunchRecord(spawned_at=time.monotonic())
    return Result(Outcome.STARTED, pid=proc.pid)


def stop(model_id: str) -> Result:
    state = _state_for(model_id)
    p = state.proc
    # A deliberate stop (even of an already-dead process) ends the launch: what
    # happens to the process afterwards is not a failure of ours.
    state.launch = None
    # Inherited backend: we don't hold a Popen handle, so polite shutdown
    # isn't an option — taskkill the PID directly.
    if p is None:
        if _inherited_alive(state):
            pid_ = state.inherited_pid
            state.forget_inherited()
            ok, msg = kill_pid(int(pid_))
            return Result(Outcome.STOPPED if ok else Outcome.STOP_FAILED, msg, int(pid_))
        return Result(Outcome.NOT_RUNNING)
    if p.poll() is not None:
        state.proc = None
        return Result(Outcome.NOT_RUNNING)
    try:
        if sys.platform == "win32":
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                pass
            else:
                try:
                    p.wait(timeout=STOP_GRACE_S)
                    state.proc = None
                    return Result(Outcome.STOPPED)
                except subprocess.TimeoutExpired:
                    pass
        p.terminate()
        try:
            p.wait(timeout=STOP_TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=STOP_KILL_WAIT_S)
    except Exception as e:
        return Result(Outcome.STOP_FAILED, f"error stopping: {e}")
    state.proc = None
    return Result(Outcome.STOPPED)


def inherit_running_backends() -> int:
    """Adopt any model-backend process the hub finds on one of its ports.

    Called once at hub startup. Without this, a hub restart leaves the
    previous hub's children alive on their ports — the new hub sees
    them as "external" (adopted) and the UI shows the disabled-Stop
    state. With inheritance, the new hub treats them as ours, the UI
    shows them as running, and Stop force-kills the PID directly.

    Returns the number of backends inherited.
    """
    from .server_process import snapshot_listening_pids

    try:
        import psutil
    except ImportError:
        return 0

    listening = snapshot_listening_pids()
    count = 0
    for m in enabled_models():
        if m.backend not in ("openai", "whisper"):
            continue
        if not m.port:
            continue
        if is_running(m.id):
            continue  # already ours (Popen or earlier-inherited)
        pids = listening.get(m.port) or []
        if not pids:
            continue
        candidate = pids[0]
        try:
            proc = psutil.Process(candidate)
            identity = _process_identity(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if _looks_like_backend_binary(identity[1], m):
            state = _state_for(m.id)
            state.inherited_pid = candidate
            state.inherited_identity = identity
            state.launch = LaunchRecord(spawned_at=time.monotonic())
            count += 1
            logger.info(
                "📎 Inherited %s on :%s (PID %s) — log tail at %s",
                m.id, m.port, candidate, _log_path(m.id),
            )
    return count


def _looks_like_backend_binary(exe: str, model: "Model") -> bool:
    """Heuristic: does this executable look like the binary we'd spawn for ``model``?"""
    exe = (exe or "").lower()
    if _is_whisper(model):
        return "whisper-server" in exe or exe.endswith("whisper-server.exe")
    # Default: llama.cpp's llama-server.
    return "llama-server" in exe


def resolve_model_by_id(model_id: str) -> Optional[Model]:
    for m in enabled_models():
        if m.id == model_id:
            return m
    return None


def running_backends() -> Dict[str, Model]:
    """Return {model_id: Model} for each local backend whose process is alive."""
    out: Dict[str, Model] = {}
    for m in enabled_models():
        if m.backend in ("openai", "whisper") and is_running(m.id):
            out[m.id] = m
    return out


def external_pid(model_id: str) -> Optional[int]:
    """PID holding *model_id*'s port if it isn't us, else ``None``."""
    model = resolve_model_by_id(model_id)
    if model is None or model.port is None:
        return None
    return resolve_external_pid(is_running(model_id), model.port)


def force_stop_external(model_id: str) -> tuple[bool, str]:
    """Force-kill whoever currently holds *model_id*'s port, if it's not us."""
    target = external_pid(model_id)
    if target is None:
        return False, "no external process on this model's port"
    return kill_pid(target)
