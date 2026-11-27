"""Run each candidate on the clips and turn what was observed into one row per candidate.

A candidate that is not installed, or that fails, gets a row with a status and
a reason and no measurement: a number appears only when it was observed.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence

import psutil

from ...no_window import NO_WINDOW
from .candidates import CUDA, Candidate
from .clips import Clip, word_errors
from .sessions import EngineError

logger = logging.getLogger(__name__)

OK, NOT_INSTALLED, FAILED = "ok", "not installed", "failed"
_SAMPLE_S = 0.25
_NVIDIA_SMI_TIMEOUT_S = 5.0
# Below this a CUDA candidate's VRAM rise is noise: the engine fell back to the CPU.
_GPU_IN_USE_MB = 100.0


class GpuProbe:
    """Whole-GPU memory and utilisation from ``nvidia-smi``; ``None`` when it cannot be read.

    Per-process VRAM is not reported under the Windows WDDM driver, so the
    benchmark measures the rise in the GPU's used memory over a baseline read
    just before the engine starts. Anything else on the GPU moves it too.
    """

    def __init__(self, index: int = 0) -> None:
        self.index = index

    def _query(self, fields: str) -> Optional[List[str]]:
        try:
            out = subprocess.run(  # noqa: S603, S607 — nvidia-smi on PATH, fixed arguments
                ["nvidia-smi", "-i", str(self.index), f"--query-gpu={fields}",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=_NVIDIA_SMI_TIMEOUT_S, creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return [part.strip() for part in out.stdout.strip().splitlines()[0].split(",")]

    def name(self) -> Optional[str]:
        parts = self._query("name,memory.total")
        return f"{parts[0]} ({parts[1]} MiB)" if parts and len(parts) == 2 else None

    def read(self) -> Optional[tuple[float, float]]:
        """``(memory used in MiB, utilisation %)``."""
        parts = self._query("memory.used,utilization.gpu")
        try:
            return (float(parts[0]), float(parts[1])) if parts and len(parts) == 2 else None
        except ValueError:
            return None


@dataclass
class Samples:
    rss_mb: List[float] = field(default_factory=list)
    cpu_cores: List[float] = field(default_factory=list)
    vram_mb: List[float] = field(default_factory=list)
    gpu_util: List[float] = field(default_factory=list)


class Sampler:
    """Polls one engine process and its children in a thread until stopped."""

    def __init__(self, pid: int, gpu: Optional[GpuProbe]) -> None:
        self.pid, self.gpu = pid, gpu
        self.samples = Samples()
        self.phase = "load"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> Samples:
        self._stop.set()
        self._thread.join(timeout=10)
        return self.samples

    def _refresh(self, procs: Dict[int, psutil.Process]) -> None:
        """Add new children; a known pid keeps its object, whose CPU interval runs from its last read."""
        try:
            root = psutil.Process(self.pid) if self.pid not in procs else procs[self.pid]
            found = [root, *root.children(recursive=True)]
        except psutil.Error:
            return
        for proc in found:
            if proc.pid not in procs:
                try:
                    proc.cpu_percent(None)  # the first call only starts the interval
                except psutil.Error:
                    continue
                procs[proc.pid] = proc

    def _run(self) -> None:
        procs: Dict[int, psutil.Process] = {}
        self._refresh(procs)
        while not self._stop.wait(_SAMPLE_S):
            rss = cpu = 0.0
            for proc in list(procs.values()):
                try:
                    rss += proc.memory_info().rss / 2**20
                    cpu += proc.cpu_percent(None) / 100.0
                except psutil.Error:
                    procs.pop(proc.pid, None)
            self.samples.rss_mb.append(rss)
            if self.phase == "transcribe":
                self.samples.cpu_cores.append(cpu)
            reading = self.gpu.read() if self.gpu else None
            if reading:
                self.samples.vram_mb.append(reading[0])
                if self.phase == "transcribe":
                    self.samples.gpu_util.append(reading[1])
            self._refresh(procs)


@dataclass
class Row:
    candidate: Candidate
    status: str
    note: str = ""
    load_s: Optional[float] = None
    rtf: Optional[float] = None
    eou_s: Optional[float] = None
    wer: Optional[float] = None
    cpu_cores: Optional[float] = None
    peak_rss_mb: Optional[float] = None
    gpu_util: Optional[float] = None
    vram_mb: Optional[float] = None
    transcripts: List[str] = field(default_factory=list)


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _summarise(row: Row, clips: Sequence[Clip], results, samples: Samples,
               baseline_vram: Optional[float]) -> None:
    audio_s = sum(c.seconds for c in clips)
    row.rtf = sum(r.seconds for r in results) / audio_s if audio_s else None
    eous = [r.eou_s for r in results if r.eou_s is not None]
    row.eou_s = _mean(eous)
    errors = refs = 0
    for clip, result in zip(clips, results):
        if clip.reference:
            e, n = word_errors(clip.reference, result.text)
            errors, refs = errors + e, refs + n
    row.wer = errors / refs if refs else None
    row.cpu_cores = _mean(samples.cpu_cores)
    row.peak_rss_mb = max(samples.rss_mb) if samples.rss_mb else None
    row.gpu_util = _mean(samples.gpu_util)
    if baseline_vram is not None and samples.vram_mb:
        row.vram_mb = max(0.0, max(samples.vram_mb) - baseline_vram)
    notes = []
    if row.wer is None:
        notes.append("no reference text")
    if row.candidate.device == CUDA and row.vram_mb is not None and row.vram_mb < _GPU_IN_USE_MB:
        notes.append("no VRAM rise: ran on the CPU")
    row.note = "; ".join(notes)
    row.transcripts = [r.text for r in results]


SamplerFactory = Callable[[int], Sampler]


def run_candidate(candidate: Candidate, clips: Sequence[Clip], *, scratch: Path,
                  avoid: FrozenSet[int], language: str, gpu: Optional[GpuProbe],
                  sampler_factory: Optional[SamplerFactory] = None) -> Row:
    """One row: ``not installed``, ``failed`` with the reason, or the measurements."""
    try:
        reason = candidate.missing()
    except Exception as exc:  # noqa: BLE001 — a broken install is reported, never fatal
        reason = f"install check failed: {type(exc).__name__}: {exc}"
    if reason:
        logger.info("ℹ️ %s: not installed (%s)", candidate.id, reason)
        return Row(candidate, NOT_INSTALLED, reason)
    make_sampler = sampler_factory or (lambda pid: Sampler(pid, gpu))
    baseline = gpu.read() if gpu else None
    session = candidate.open(scratch, avoid)
    sampler: Optional[Sampler] = None
    try:
        started = time.perf_counter()
        sampler = make_sampler(session.start())
        sampler.start()
        session.wait_ready()
        load_s = time.perf_counter() - started
        session.transcribe(clips[0].path, language)  # warm-up, not counted
        sampler.phase = "transcribe"
        results = [session.transcribe(clip.path, language) for clip in clips]
        samples = sampler.stop()
    except (EngineError, OSError, ValueError) as exc:
        logger.error("❌ %s failed: %s", candidate.id, exc)
        return Row(candidate, FAILED, str(exc))
    finally:
        if sampler:
            sampler.stop()
        session.stop()
    row = Row(candidate, OK, load_s=load_s)
    _summarise(row, clips, results, samples, baseline[0] if baseline else None)
    logger.info("✅ %s: load %.1f s, RTF %.3f", candidate.id, load_s, row.rtf or 0.0)
    return row


def run_all(candidates: Sequence[Candidate], clips: Sequence[Clip], *, avoid: FrozenSet[int],
            language: str, gpu: Optional[GpuProbe],
            sampler_factory: Optional[SamplerFactory] = None) -> List[Row]:
    """Each candidate in turn, so no two engines compete for the CPU or the GPU."""
    with tempfile.TemporaryDirectory(prefix="asr-bench-") as tmp:
        return [run_candidate(c, clips, scratch=Path(tmp), avoid=avoid, language=language,
                              gpu=gpu, sampler_factory=sampler_factory) for c in candidates]
