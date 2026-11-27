"""The candidate engines, whether each is installed here, and how to start a disposable one.

Every candidate reads its files under one root folder (``vendor/`` and
``models/`` of a checkout; this checkout unless ``--root`` names another), so
nothing is assumed about where another app keeps its models. ``models/bench/``
holds what only the benchmark uses; ``python -m src.audio.bench fetch`` fills it.
"""

from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional

import yaml

from ... import host_profile
from ...model_registry import ENGINE_FOUNDRY, Model, all_models
from ...runtime_files import checked_write_path
from .sessions import LOOPBACK, HttpServerSession, RunnerSession, Session, free_port

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BENCH_MODELS = Path("models") / "bench"

CPU, CUDA = "cpu", "cuda"

WHISPER_WEIGHTS = {
    "f16": "ggml-large-v3-turbo.bin",
    "q8_0": "ggml-large-v3-turbo-q8_0.bin",
    "q5_0": "ggml-large-v3-turbo-q5_0.bin",
}
SHERPA_LATENCIES = ("560ms", "240ms", "1120ms")
SHERPA_DIR = "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-{latency}"
FASTER_WHISPER_DIR = "faster-whisper-large-v3-turbo"
FOUNDRY_ROWS = ("nemotron_asr", "parakeet_asr")
FETCH_HINT = "run `python -m src.audio.bench fetch`"

# open(scratch_dir, ports_to_avoid) -> a session not yet started.
Opener = Callable[[Path, FrozenSet[int]], Session]


@dataclass(frozen=True)
class Candidate:
    id: str
    engine: str
    device: str
    streaming: bool
    missing: Callable[[], Optional[str]]
    open: Opener


def _python_env(extra_path: Optional[Path] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if extra_path is not None:
        # The CUDA runtime DLLs ship beside whisper-server.exe.
        env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
    return env


def _row(model_id: str) -> Optional[Model]:
    return next((m for m in all_models() if m.id == model_id), None)


# --- whisper.cpp ------------------------------------------------------------

def _whisper_binary(root: Path) -> Path:
    name = "whisper-server.exe" if sys.platform == "win32" else "whisper-server"
    return root / "vendor" / "whisper.cpp" / name


def _whisper(root: Path, quant: str, device: str) -> Candidate:
    weights = root / "models" / WHISPER_WEIGHTS[quant]
    binary = _whisper_binary(root)

    def missing() -> Optional[str]:
        if not binary.is_file():
            return "whisper-server not installed (python scripts/install_whisper_cpp.py)"
        if not weights.is_file():
            return f"{weights.name} not downloaded ({FETCH_HINT})"
        return None

    def open_session(scratch: Path, avoid: FrozenSet[int]) -> Session:
        port = free_port(avoid)
        row = _row("whisper")
        # The registry row's own args, so the production decoding guards are measured.
        cmd = [str(binary), "--host", LOOPBACK, "--port", str(port), "--model", str(weights),
               *(row.args if row else []), *(["-ng"] if device == CPU else [])]
        return HttpServerSession(cmd, port=port, model_name="whisper", env=_python_env(binary.parent),
                                 cwd=root, log_path=scratch / f"whisper-{quant}-{device}.log")

    return Candidate(f"whisper-turbo-{quant}-{device}", f"whisper.cpp large-v3-turbo {quant}",
                     device, False, missing, open_session)


# --- Foundry Local ----------------------------------------------------------

def _scratch_config(scratch: Path, model: Model, port: int) -> Path:
    """This PC's merged registry with only ``model`` enabled, on a port of its own."""
    config = copy.deepcopy(host_profile._load_config())
    config["hosts"] = {"local": {"enabled": [model.id]}}
    config["models"][model.id]["port"] = port
    path = checked_write_path(scratch / f"models-{model.id}.yaml")
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _foundry(row_id: str) -> Candidate:
    def missing() -> Optional[str]:
        from ...foundry import runtime

        model = _row(row_id)
        if model is None or model.engine != ENGINE_FOUNDRY:
            return f"no `{row_id}` foundry-local row in the registry"
        if runtime.sdk_version() is None:
            return "Foundry Local SDK not installed (python -m src.foundry setup)"
        try:
            if not runtime.is_cached(model.runtime_model or ""):
                return f"{model.runtime_model} not cached (docs/foundry-local.md)"
        except runtime.FoundryError as exc:
            return f"Foundry Local catalog unavailable: {exc}"
        return None

    def open_session(scratch: Path, avoid: FrozenSet[int]) -> Session:
        from ...process.backend_process import FOUNDRY_WORKER_MODULE

        model = _row(row_id)
        assert model is not None
        port = free_port(avoid)
        config = _scratch_config(scratch, model, port)
        cmd = [sys.executable, "-m", FOUNDRY_WORKER_MODULE, model.id, "--config", str(config)]
        return HttpServerSession(cmd, port=port, model_name=model.display_name, env=_python_env(),
                                 cwd=PROJECT_ROOT, log_path=scratch / f"{row_id}.log")

    label = {"nemotron_asr": "Foundry Nemotron 3.5 ASR", "parakeet_asr": "Foundry Parakeet TDT 0.6b v2"}
    model = _row(row_id)
    streaming = bool(model and model.asr_mode == "streaming")
    return Candidate(row_id.replace("_", "-"), label[row_id], CPU, streaming, missing, open_session)


# --- Python-library engines (src.audio.bench.runner) -------------------------

def _package_missing(module: str, install: str) -> Optional[str]:
    if importlib.util.find_spec(module) is None:
        return f"{module} not installed ({install})"
    return None


def _runner(engine: str, model_dir: Path, device: str, log_name: str) -> Opener:
    def open_session(scratch: Path, avoid: FrozenSet[int]) -> Session:
        cmd = [sys.executable, "-m", "src.audio.bench.runner", "--engine", engine,
               "--model-dir", str(model_dir), "--provider", device]
        return RunnerSession(cmd, env=_python_env(), cwd=PROJECT_ROOT, log_path=scratch / log_name)

    return open_session


def _sherpa(root: Path, latency: str, device: str) -> Candidate:
    model_dir = root / BENCH_MODELS / SHERPA_DIR.format(latency=latency)

    def missing() -> Optional[str]:
        reason = _package_missing("sherpa_onnx", "docs/asr-benchmark.md")
        if reason:
            return reason
        if device == CUDA and "cuda" not in importlib.metadata.version("sherpa-onnx"):
            return "the installed sherpa-onnx is the CPU-only build"
        if not (model_dir / "encoder.int8.onnx").is_file():
            return f"{model_dir.name} not downloaded ({FETCH_HINT})"
        return None

    return Candidate(f"parakeet-unified-{latency}-{device}",
                     f"sherpa-onnx Parakeet Unified int8 {latency}", device, True, missing,
                     _runner("sherpa", model_dir, device, f"sherpa-{latency}-{device}.log"))


def _faster_whisper(root: Path, device: str) -> Candidate:
    model_dir = root / BENCH_MODELS / FASTER_WHISPER_DIR

    def missing() -> Optional[str]:
        reason = _package_missing("faster_whisper", "docs/asr-benchmark.md")
        if reason:
            return reason
        if device == CUDA:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() == 0:
                return "CTranslate2 sees no CUDA device"
        if not (model_dir / "model.bin").is_file():
            return f"{model_dir.name} not downloaded ({FETCH_HINT})"
        return None

    return Candidate(f"faster-whisper-turbo-int8-{device}", "faster-whisper large-v3-turbo int8",
                     device, False, missing, _runner("faster-whisper", model_dir, device,
                                                     f"faster-whisper-{device}.log"))


def catalog(root: Path) -> List[Candidate]:
    """Every candidate, in the order the table lists them: today's default first."""
    out: List[Candidate] = [_foundry(row) for row in FOUNDRY_ROWS]
    for latency in SHERPA_LATENCIES:
        out += [_sherpa(root, latency, CPU), _sherpa(root, latency, CUDA)]
    for quant in WHISPER_WEIGHTS:
        out += [_whisper(root, quant, CUDA), _whisper(root, quant, CPU)]
    out += [_faster_whisper(root, CUDA), _faster_whisper(root, CPU)]
    return out


def registry_ports() -> FrozenSet[int]:
    """Every port the registry names, so a disposable engine never takes a live backend's."""
    ports = {m.port for m in all_models() if m.port}
    ports.add(host_profile.hub_port())
    return frozenset(ports)
