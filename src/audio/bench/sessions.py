"""One disposable engine process per candidate, and how the benchmark talks to it.

Two kinds, each a child process the benchmark spawned itself and alone stops:

- :class:`HttpServerSession`: a server on a free loopback port (whisper-server,
  the Foundry worker), ready when ``GET /health`` answers 200, transcribing
  through the OpenAI ``POST /v1/audio/transcriptions`` shape the hub uses.
- :class:`RunnerSession`: ``python -m src.audio.bench.runner`` for an engine
  that is a Python library (sherpa-onnx, faster-whisper), driven by JSON lines
  on stdin and stdout.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List, Optional, Protocol

import httpx

from ...no_window import NO_WINDOW
from ...runtime_files import checked_write_path

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
# A model loading from a cold disk on a slow PC (the 0.6 B ONNX exports, or
# whisper large-v3-turbo on CPU) can take minutes; past this it is stuck.
READY_TIMEOUT_S = 600.0
# One clip of a few minutes on CPU at a real-time factor near 1.
TRANSCRIBE_TIMEOUT_S = 900.0
_POLL_S = 0.5
# How long a stopped engine gets to exit on its own before it is killed.
_STOP_WAIT_S = 10.0
_LOG_TAIL = 15


class EngineError(RuntimeError):
    """The engine failed to start or to transcribe; the message says which and why."""


@dataclass(frozen=True)
class Transcript:
    text: str
    seconds: float
    eou_s: Optional[float] = None


class Session(Protocol):
    def start(self) -> int: ...
    def wait_ready(self) -> None: ...
    def transcribe(self, clip: Path, language: str) -> Transcript: ...
    def stop(self) -> None: ...


def free_port(avoid: frozenset[int] = frozenset()) -> int:
    """A loopback port the OS reports free, never one of ``avoid`` (the registry's own ports)."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOOPBACK, 0))
            port = sock.getsockname()[1]
        if port not in avoid:
            return port


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(line.strip() for line in lines[-_LOG_TAIL:] if line.strip())


class _ChildProcess:
    """Spawn, log to a file, and stop exactly the one process this session started."""

    def __init__(self, cmd: List[str], *, env: Dict[str, str], cwd: Path, log_path: Path) -> None:
        self.cmd, self.env, self.cwd, self.log_path = cmd, env, cwd, log_path
        self.proc: Optional[subprocess.Popen] = None
        self._log: Optional[IO[bytes]] = None

    def spawn(self, *, stdin: Optional[int] = None, stdout: Optional[int] = None) -> int:
        self._log = open(checked_write_path(self.log_path), "wb")  # noqa: SIM115 — closed in stop()
        self.proc = subprocess.Popen(  # noqa: S603 — a fixed engine command, no shell
            self.cmd, cwd=self.cwd, env=self.env, stdin=stdin,
            stdout=stdout if stdout is not None else self._log, stderr=self._log,
            creationflags=NO_WINDOW,
        )
        logger.info("ℹ️ started pid %s: %s", self.proc.pid, " ".join(self.cmd))
        return self.proc.pid

    def exited(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    def failure(self, what: str) -> EngineError:
        code = self.exited()
        detail = f"exited with code {code}" if code is not None else "still running"
        return EngineError(f"{what} ({detail}); log: {_log_tail(self.log_path) or 'empty'}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                logger.warning("⚠️ pid %s ignored terminate; killing it", self.proc.pid)
                self.proc.kill()
                self.proc.wait(timeout=_STOP_WAIT_S)
        if self._log:
            self._log.close()
            self._log = None


class HttpServerSession:
    """An OpenAI-shape transcription server the benchmark spawned on its own port."""

    def __init__(self, cmd: List[str], *, port: int, model_name: str, env: Dict[str, str],
                 cwd: Path, log_path: Path, ready_timeout_s: float = READY_TIMEOUT_S) -> None:
        self.child = _ChildProcess(cmd, env=env, cwd=cwd, log_path=log_path)
        self.base = f"http://{LOOPBACK}:{port}"
        self.model_name = model_name
        self.ready_timeout_s = ready_timeout_s

    def start(self) -> int:
        return self.child.spawn()

    def wait_ready(self) -> None:
        deadline = time.monotonic() + self.ready_timeout_s
        while time.monotonic() < deadline:
            if self.child.exited() is not None:
                raise self.child.failure("server stopped before it was ready")
            try:
                if httpx.get(f"{self.base}/health", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        raise self.child.failure(f"server not ready after {self.ready_timeout_s:.0f} s")

    def transcribe(self, clip: Path, language: str) -> Transcript:
        data = {"model": self.model_name, "response_format": "json", "language": language}
        started = time.perf_counter()
        try:
            with clip.open("rb") as fh:
                resp = httpx.post(f"{self.base}/v1/audio/transcriptions", data=data,
                                  files={"file": (clip.name, fh, "audio/wav")},
                                  timeout=TRANSCRIBE_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise self.child.failure(f"transcription request failed: {exc}") from exc
        seconds = time.perf_counter() - started
        if resp.status_code != 200:
            raise EngineError(f"transcription answered {resp.status_code}: {resp.text[:300]}")
        return Transcript(str(resp.json().get("text", "")).strip(), seconds)

    def stop(self) -> None:
        self.child.stop()


class RunnerSession:
    """``src.audio.bench.runner`` in a child process: one JSON request and reply per line."""

    def __init__(self, cmd: List[str], *, env: Dict[str, str], cwd: Path, log_path: Path,
                 ready_timeout_s: float = READY_TIMEOUT_S) -> None:
        self.child = _ChildProcess(cmd, env=env, cwd=cwd, log_path=log_path)
        self.ready_timeout_s = ready_timeout_s
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def start(self) -> int:
        pid = self.child.spawn(stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        return pid

    def _read(self) -> None:
        assert self.child.proc and self.child.proc.stdout
        for raw in self.child.proc.stdout:
            self._lines.put(raw.decode("utf-8", errors="replace"))
        self._lines.put(None)

    def _reply(self, timeout_s: float, what: str) -> dict:
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            raise self.child.failure(f"no answer to {what} after {timeout_s:.0f} s") from None
        if line is None:
            raise self.child.failure(f"runner closed its output during {what}")
        reply = json.loads(line)
        if reply.get("event") == "error":
            raise EngineError(f"{what} failed: {reply.get('message')}")
        return reply

    def wait_ready(self) -> None:
        self._reply(self.ready_timeout_s, "the model load")

    def transcribe(self, clip: Path, language: str) -> Transcript:
        assert self.child.proc and self.child.proc.stdin
        self.child.proc.stdin.write((json.dumps({"path": str(clip), "language": language}) + "\n").encode())
        self.child.proc.stdin.flush()
        reply = self._reply(TRANSCRIBE_TIMEOUT_S, f"transcribing {clip.name}")
        return Transcript(str(reply.get("text", "")).strip(), float(reply["seconds"]),
                          reply.get("eou_s"))

    def stop(self) -> None:
        if self.child.proc and self.child.proc.stdin and self.child.exited() is None:
            try:
                self.child.proc.stdin.close()
            except OSError:
                pass
        self.child.stop()
