"""Child process for a candidate that is a Python library: ``python -m src.audio.bench.runner``.

Loads one engine, prints ``{"event": "ready"}``, then answers each stdin line
``{"path": ..., "language": ...}`` with ``{"text", "seconds", "eou_s"}`` on
stdout. Logs go to stderr. A load or transcription failure is a
``{"event": "error", "message": ...}`` line, so the benchmark reports it.

- ``sherpa``: a sherpa-onnx streaming transducer (the Parakeet Unified
  exports). The clip is fed in 0.1 s chunks as fast as the engine decodes, so
  ``seconds`` is compute time, not wall-clock playback. ``eou_s`` is the time
  from the end of the audio (tail padding plus ``input_finished``) to the final
  text: the compute part of the end-of-utterance delay, which a live stream
  adds to the export's chunk latency.
- ``faster-whisper``: a CTranslate2 Whisper model, transcribed whole.
"""

from __future__ import annotations

import argparse
import array
import json
import logging
import sys
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 0.1 s per accept_waveform call, the granularity a microphone feed delivers.
_CHUNK_SAMPLES = 1600
# Silence appended before input_finished so the last chunk's right context fills
# (the 0.66 s the sherpa-onnx streaming examples pad with).
_TAIL_SECONDS = 0.66
_SHERPA_FEATURE_DIM = 128
_THREADS = 4


def read_samples(path: Path) -> List[float]:
    """16-bit mono PCM as floats in [-1, 1)."""
    with wave.open(str(path)) as wav:
        pcm = array.array("h", wav.readframes(wav.getnframes()))
    if sys.byteorder == "big":
        pcm.byteswap()
    return [s / 32768.0 for s in pcm]


class SherpaEngine:
    def __init__(self, model_dir: Path, provider: str) -> None:
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            num_threads=_THREADS, sample_rate=SAMPLE_RATE, feature_dim=_SHERPA_FEATURE_DIM,
            decoding_method="greedy_search", provider=provider,
        )

    def transcribe(self, path: Path, language: str) -> Dict[str, Any]:
        rec = self.recognizer
        samples = read_samples(path)
        started = time.perf_counter()
        stream = rec.create_stream()
        for i in range(0, len(samples), _CHUNK_SAMPLES):
            stream.accept_waveform(SAMPLE_RATE, samples[i:i + _CHUNK_SAMPLES])
            while rec.is_ready(stream):
                rec.decode_stream(stream)
        end_of_audio = time.perf_counter()
        stream.accept_waveform(SAMPLE_RATE, [0.0] * int(_TAIL_SECONDS * SAMPLE_RATE))
        stream.input_finished()
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        done = time.perf_counter()
        result = rec.get_result(stream)
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return {"text": text, "seconds": done - started, "eou_s": done - end_of_audio}


class FasterWhisperEngine:
    def __init__(self, model_dir: Path, provider: str) -> None:
        from faster_whisper import WhisperModel

        # Pascal has no fast fp16 path in CTranslate2, so int8 on both devices.
        self.model = WhisperModel(str(model_dir), device=provider, compute_type="int8",
                                  cpu_threads=_THREADS)

    def transcribe(self, path: Path, language: str) -> Dict[str, Any]:
        started = time.perf_counter()
        segments, _info = self.model.transcribe(str(path), language=language or None, beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments)
        return {"text": text, "seconds": time.perf_counter() - started, "eou_s": None}


ENGINES: Dict[str, Callable[[Path, str], Any]] = {
    "sherpa": SherpaEngine,
    "faster-whisper": FasterWhisperEngine,
}


def serve(engine: Any, lines, write: Callable[[str], None]) -> None:
    """Answer each JSON request line from ``lines`` with one JSON reply through ``write``."""
    write(json.dumps({"event": "ready"}))
    for line in lines:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            reply = engine.transcribe(Path(request["path"]), request.get("language") or "")
        except Exception as exc:  # noqa: BLE001 — reported to the benchmark as this clip's failure
            reply = {"event": "error", "message": f"{type(exc).__name__}: {exc}"}
        write(json.dumps(reply))


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.audio.bench.runner")
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=("cpu", "cuda"), required=True)
    args = parser.parse_args(argv)

    def write(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    try:
        engine = ENGINES[args.engine](args.model_dir, args.provider)
    except Exception as exc:  # noqa: BLE001 — a load failure is the result being measured
        logger.error("❌ %s load failed: %s", args.engine, exc)
        write(json.dumps({"event": "error", "message": f"{type(exc).__name__}: {exc}"}))
        return 3
    logger.info("✅ %s loaded from %s on %s", args.engine, args.model_dir, args.provider)
    serve(engine, sys.stdin, write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
