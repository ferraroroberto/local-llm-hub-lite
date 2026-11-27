"""The benchmark's one Markdown table, headed by the machine it ran on (no host name)."""

from __future__ import annotations

import os
import platform
from typing import List, Optional, Sequence

import psutil

from .clips import Clip
from .measure import OK, Row

NO_VALUE = "—"
COLUMNS = ("Candidate", "Device", "Status", "Cold load (s)", "Warm RTF", "EOU finalize (s)",
           "WER", "CPU (cores)", "Peak RAM (MB)", "GPU util (%)", "VRAM rise (MB)", "Note")


def _fmt(value: Optional[float], spec: str) -> str:
    return NO_VALUE if value is None else format(value, spec)


def _cells(row: Row) -> List[str]:
    c = row.candidate
    head = [c.id, c.device, row.status]
    if row.status != OK:
        # Not measured, so no number: only the reason.
        return [*head, *([NO_VALUE] * 8), row.note.replace("|", "/")]
    return [*head, _fmt(row.load_s, ".1f"), _fmt(row.rtf, ".3f"),
            _fmt(row.eou_s, ".3f") if c.streaming else "n/a",
            _fmt(None if row.wer is None else row.wer * 100, ".1f") + ("%" if row.wer is not None else ""),
            _fmt(row.cpu_cores, ".2f"), _fmt(row.peak_rss_mb, ".0f"), _fmt(row.gpu_util, ".0f"),
            _fmt(row.vram_mb, ".0f"), row.note.replace("|", "/")]


def machine_line(gpu_name: Optional[str]) -> str:
    ram_gb = psutil.virtual_memory().total / 2**30
    cpu = platform.processor() or platform.machine()
    return (f"CPU: {cpu}, {os.cpu_count()} logical cores; RAM: {ram_gb:.0f} GB; "
            f"GPU: {gpu_name or 'none found by nvidia-smi'}; OS: {platform.system()} {platform.release()}")


def render(rows: Sequence[Row], clips: Sequence[Clip], gpu_name: Optional[str]) -> str:
    audio_s = sum(c.seconds for c in clips)
    with_ref = sum(1 for c in clips if c.reference)
    lines = [
        f"Machine: {machine_line(gpu_name)}",
        f"Clips: {len(clips)} ({audio_s:.1f} s of audio, {with_ref} with a reference transcript)",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    lines += ["| " + " | ".join(_cells(r)) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def transcripts(rows: Sequence[Row], clips: Sequence[Clip]) -> str:
    """Each measured candidate's text per clip, for checking the WER by eye."""
    out: List[str] = []
    for row in rows:
        if row.status != OK:
            continue
        out.append(f"## {row.candidate.id}")
        out += [f"- {clip.path.name}: {text}" for clip, text in zip(clips, row.transcripts)]
        out.append("")
    return "\n".join(out)
