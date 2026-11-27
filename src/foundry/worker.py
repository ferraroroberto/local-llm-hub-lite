"""Foundry Local ASR worker: one process per ``engine: foundry-local`` row.

``python -m src.foundry.worker <model_id>`` resolves the row's exact
``runtime_model`` in the Foundry Local catalog, starts it (or reuses it when
already loaded), then serves on the row's loopback port:

- ``GET /health``: ``engine`` and ``model`` (the exact catalog variant), so a
  client sharing the port (Voice Transcriber) can confirm what answers
  before adopting it, not only that something does; ``model_cache`` names the
  cache folder, which setting chose it and whether it is still there.
- ``GET /v1/models``: the one variant served.
- ``POST /v1/audio/transcriptions``: the OpenAI multipart shape, 16-bit PCM
  WAV in, ``{"text": ...}`` out.

The row's ``asr_mode`` picks the SDK call. The streaming Nemotron ASR
variants reject file transcription, so their WAV is fed through a live
transcription session as PCM chunks. An ``offline`` recognizer (Parakeet)
rejects that session and transcribes the whole file in one call.

The hub spawns this module like any backend (``backend_process``); the
model loads before the port opens, so a reachable ``/health`` means ready.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Iterable, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..model_registry import ASR_OFFLINE, ENGINE_FOUNDRY, Model
from . import runtime

logger = logging.getLogger(__name__)

# 16-bit PCM is what the live session is configured for (``bits_per_sample``).
_PCM_SAMPLE_WIDTH = 2
# 0.1 s of 16 kHz mono 16-bit audio per append: small enough that the SDK's
# bounded push queue never blocks for long, large enough to keep calls few.
_PCM_CHUNK_BYTES = 3200
_RESPONSE_FORMATS = ("json", "text", "verbose_json")


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int]:
    """``(pcm_frames, sample_rate, channels)`` from a 16-bit PCM WAV, else ``ValueError``."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if wav.getsampwidth() != _PCM_SAMPLE_WIDTH:
                raise ValueError(
                    f"WAV is {wav.getsampwidth() * 8}-bit; send 16-bit PCM"
                )
            return wav.readframes(wav.getnframes()), wav.getframerate(), wav.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a PCM WAV file ({exc})") from exc


def join_results(results: Iterable[Any]) -> str:
    """The transcript from live-session results: the final segments, else the partials."""
    finals: List[str] = []
    partials: List[str] = []
    for result in results:
        text = "".join(part.text for part in (result.content or []))
        (finals if result.is_final else partials).append(text)
    return " ".join(t.strip() for t in (finals or partials) if t.strip())


def transcribe_pcm(
    variant: Any, pcm: bytes, *, sample_rate: int, channels: int, language: Optional[str]
) -> str:
    """Push ``pcm`` through one live transcription session and return its text."""
    session = variant.get_audio_client().create_live_transcription_session()
    session.settings.sample_rate = sample_rate
    session.settings.channels = channels
    if language:
        session.settings.language = language
    session.start()
    results: List[Any] = []
    errors: List[BaseException] = []

    # The push queue is bounded, so results are drained on their own thread
    # while this one appends; otherwise a long clip deadlocks the session.
    def _drain() -> None:
        try:
            results.extend(session.get_stream())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the request thread
            errors.append(exc)

    reader = threading.Thread(target=_drain, name="foundry-asr-drain", daemon=True)
    reader.start()
    try:
        for offset in range(0, len(pcm), _PCM_CHUNK_BYTES):
            session.append(pcm[offset:offset + _PCM_CHUNK_BYTES])
    finally:
        session.stop()
        reader.join()
    if errors:
        raise errors[0]
    return join_results(results)


def transcribe_file(variant: Any, wav: bytes, *, language: Optional[str]) -> str:
    """Hand the whole ``wav`` to an offline recognizer and return its text."""
    client = variant.get_audio_client()
    if language:
        client.settings.language = language
    # The SDK takes a path, not bytes, so the upload lives in a temp folder
    # for the length of the call.
    with tempfile.TemporaryDirectory(prefix="foundry-asr-") as scratch:
        path = Path(scratch) / "upload.wav"
        path.write_bytes(wav)
        return client.transcribe(str(path)).text.strip()


def build_app(model: Model, variant: Any) -> FastAPI:
    """The worker's HTTP surface for ``model`` served by the loaded ``variant``."""
    app = FastAPI(title=f"foundry-local worker: {model.id}")
    # One transcription at a time: the variant is a single in-process model.
    lock = threading.Lock()
    # Fixed at SDK initialisation, so read once; only the folder state is live.
    cache = runtime.model_cache()
    identity = {
        "engine": ENGINE_FOUNDRY,
        "model": model.runtime_model,
        "model_id": model.id,
        "display_name": model.display_name,
        "sdk_version": runtime.sdk_version(),
        "pid": os.getpid(),
    }

    @app.get("/health")
    def health() -> dict:
        model_cache = {"path": str(cache.path), "source": cache.source, "state": cache.state()}
        return {"status": "ok", **identity, "model_cache": model_cache}

    @app.get("/v1/models")
    def models() -> dict:
        return {
            "object": "list",
            "data": [{"id": model.runtime_model, "object": "model", "owned_by": ENGINE_FOUNDRY}],
        }

    @app.post("/v1/audio/transcriptions")
    def transcriptions(
        file: UploadFile = File(...),
        language: Optional[str] = Form(None),
        response_format: str = Form("json"),
        task: Optional[str] = Form(None),
    ) -> Response:
        if task and task != "transcribe":
            raise HTTPException(400, f"task {task!r} is not supported by {model.runtime_model}")
        if response_format not in _RESPONSE_FORMATS:
            raise HTTPException(
                400, f"response_format {response_format!r} not supported; use one of {_RESPONSE_FORMATS}"
            )
        try:
            wav = file.file.read()
            pcm, rate, channels = wav_to_pcm(wav)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        with lock:
            if model.asr_mode == ASR_OFFLINE:
                text = transcribe_file(variant, wav, language=language)
            else:
                text = transcribe_pcm(
                    variant, pcm, sample_rate=rate, channels=channels, language=language
                )
        if response_format == "text":
            return PlainTextResponse(text)
        body: dict = {"text": text}
        if response_format == "verbose_json":
            body.update(language=language or "", duration=len(pcm) / (rate * channels * _PCM_SAMPLE_WIDTH))
        return JSONResponse(body)

    return app


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stdout)
    parser = argparse.ArgumentParser(prog="python -m src.foundry.worker")
    parser.add_argument("model_id", help="registry id of an engine: foundry-local row")
    parser.add_argument("--config", help="models.yaml the spawning hub loaded (its overlay beside it)")
    args = parser.parse_args(argv)
    if args.config:
        from .. import host_profile

        host_profile.CONFIG_PATH = Path(args.config)

    from ..process.backend_process import BACKEND_LISTEN_HOST, resolve_model_by_id

    model = resolve_model_by_id(args.model_id)
    if model is None or model.engine != ENGINE_FOUNDRY or not model.port:
        logger.error("❌ %s is not an enabled foundry-local row with a port", args.model_id)
        return 2
    try:
        variant, outcome = runtime.ensure_loaded(model.runtime_model or "")
    except runtime.FoundryError as exc:
        logger.error("❌ %s: %s", model.id, exc)
        return 3
    logger.info("✅ %s %s %s; serving on %s:%s (sdk %s)", model.id, outcome, variant.id,
                BACKEND_LISTEN_HOST, model.port, runtime.sdk_version())

    import uvicorn

    try:
        uvicorn.run(build_app(model, variant), host=BACKEND_LISTEN_HOST, port=model.port,
                    log_level="info")
    finally:
        try:
            variant.unload()
        except Exception as exc:  # noqa: BLE001 — the process is exiting either way
            logger.warning("⚠️ unload of %s failed: %s", variant.id, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
