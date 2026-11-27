"""The Foundry ASR worker's HTTP surface, driven with a faked loaded variant."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.foundry import worker
from src.model_registry import ASR_OFFLINE, ENGINE_FOUNDRY, Model
from tests.foundry._fake_sdk import (
    ASR_VARIANT,
    OFFLINE_VARIANT,
    FakeVariant,
    Part,
    Result,
)

ASR_ROW = Model(id="nemotron_asr", display_name="nemotron-3.5-asr", backend="whisper",
                engine=ENGINE_FOUNDRY, port=18090, runtime_model=ASR_VARIANT)


def _wav(seconds: float = 0.5, rate: int = 16000, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(rate * seconds) * width)
    return buf.getvalue()


@pytest.fixture
def client_and_variant():
    variant = FakeVariant(ASR_VARIANT, is_loaded=True)
    return TestClient(worker.build_app(ASR_ROW, variant)), variant


def test_health_reports_engine_and_exact_model(client_and_variant) -> None:
    client, _ = client_and_variant
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert (body["engine"], body["model"], body["model_id"]) == (ENGINE_FOUNDRY, ASR_VARIANT, "nemotron_asr")


def test_models_lists_the_one_variant(client_and_variant) -> None:
    client, _ = client_and_variant
    assert [m["id"] for m in client.get("/v1/models").json()["data"]] == [ASR_VARIANT]


def test_transcription_streams_the_wav_through_a_live_session(client_and_variant) -> None:
    client, variant = client_and_variant
    wav = _wav()
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")},
                    data={"language": "en", "response_format": "json"})
    assert r.status_code == 200
    session = variant.sessions[0]
    assert (session.settings.sample_rate, session.settings.language) == (16000, "en")
    assert r.json() == {"text": f"heard {16000} bytes"}  # final segment wins over partials


def test_text_response_format_is_plain(client_and_variant) -> None:
    client, _ = client_and_variant
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", _wav(), "audio/wav")},
                    data={"response_format": "text"})
    assert r.status_code == 200 and r.text == "heard 16000 bytes"


@pytest.mark.parametrize("payload, status", [
    (b"not a wav", 415),
    (_wav(width=1), 415),
])
def test_non_pcm16_audio_is_unsupported_media(client_and_variant, payload: bytes, status: int) -> None:
    client, _ = client_and_variant
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.bin", payload, "audio/wav")})
    assert r.status_code == status


def test_translate_task_is_refused(client_and_variant) -> None:
    client, _ = client_and_variant
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", _wav(), "audio/wav")},
                    data={"task": "translate"})
    assert r.status_code == 400


def test_offline_row_transcribes_the_whole_file_without_a_live_session() -> None:
    row = Model(id="parakeet_asr", display_name="parakeet-tdt-0.6b-v2", backend="whisper",
                engine=ENGINE_FOUNDRY, port=18024, runtime_model=OFFLINE_VARIANT, asr_mode=ASR_OFFLINE)
    variant = FakeVariant(OFFLINE_VARIANT, is_loaded=True)
    client = TestClient(worker.build_app(row, variant))
    wav = _wav()
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")},
                    data={"language": "en"})
    assert r.json() == {"text": f"read {len(wav)} bytes"}
    [(path, data, language)] = variant.files
    assert (data, language, variant.sessions) == (wav, "en", [])
    assert not Path(path).exists()  # the temp copy does not outlive the request
    health = client.get("/health").json()
    assert (health["model"], health["model_id"]) == (OFFLINE_VARIANT, "parakeet_asr")
    assert "state" in health["model_cache"]


def test_join_results_falls_back_to_partials_without_a_final() -> None:
    assert worker.join_results([Result([Part(" a")], False), Result([Part(" b ")], False)]) == "a b"
