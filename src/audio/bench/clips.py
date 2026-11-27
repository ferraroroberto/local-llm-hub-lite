"""Benchmark inputs: 16 kHz mono 16-bit WAV clips with optional reference text, and word error rate."""

from __future__ import annotations

import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2
_WORD = re.compile(r"[a-z0-9']+")


class ClipError(ValueError):
    """A clip folder or file the benchmark cannot use."""


@dataclass(frozen=True)
class Clip:
    path: Path
    seconds: float
    reference: Optional[str]


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path)) as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, 1, _SAMPLE_WIDTH):
                raise ClipError(
                    f"{path.name}: {wav.getframerate()} Hz, {wav.getnchannels()} channel(s), "
                    f"{wav.getsampwidth() * 8}-bit; every engine gets the same input, so convert it "
                    f"to 16 kHz mono 16-bit PCM (ffmpeg -i in.m4a -ar 16000 -ac 1 -c:a pcm_s16le out.wav)"
                )
            return wav.getnframes() / SAMPLE_RATE
    except (wave.Error, EOFError) as exc:
        raise ClipError(f"{path.name}: not a PCM WAV file ({exc})") from exc


def load_clips(folder: Path) -> List[Clip]:
    """Every ``*.wav`` in ``folder``, sorted, each with its same-stem ``.txt`` reference when present."""
    if not folder.is_dir():
        raise ClipError(f"clip folder not found: {folder}")
    clips = []
    for path in sorted(folder.glob("*.wav")):
        ref_path = path.with_suffix(".txt")
        reference = ref_path.read_text(encoding="utf-8").strip() if ref_path.is_file() else None
        clips.append(Clip(path, _duration(path), reference or None))
    if not clips:
        raise ClipError(f"no .wav clips in {folder}")
    return clips


def words(text: str) -> List[str]:
    """Lowercased words without punctuation, so formatting differences are not counted as errors."""
    return _WORD.findall(text.lower().replace("’", "'"))


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """``(edit distance in words, reference word count)``."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[len(hyp)], len(ref)
