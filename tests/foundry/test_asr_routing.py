"""ASR routing with a Foundry row: the role target first, Foundry before whisper.cpp, refusals loud."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from src import model_registry
from src.audio import server_audio_asr
from tests.foundry._fake_sdk import ASR_VARIANT

WHISPER = {"display_name": "whisper-large-v3-turbo", "backend": "whisper",
           "engine": "whisper-server", "port": 18022}
NEMOTRON = {"display_name": "nemotron-3.5-asr", "backend": "whisper", "engine": "foundry-local",
            "runtime_model": ASR_VARIANT, "port": 18090}


def _config(write_config, *, enabled, role=None, nemotron=None) -> None:
    cfg = {
        "hosts": {"local": {"enabled": enabled}},
        "models": {"whisper": dict(WHISPER), "nemotron_asr": nemotron or dict(NEMOTRON)},
    }
    if role:
        cfg["roles"] = {"audio": {"transcribe": {"model_id": role}}}
    write_config(cfg)


def test_role_target_nemotron_is_routed_before_whisper(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="nemotron_asr")
    assert server_audio_asr._local_asr().id == "nemotron_asr"
    assert server_audio_asr._whisper_model_for_request("audio_transcribe").id == "nemotron_asr"
    assert server_audio_asr._whisper_model_for_request("whisper-1").id == "nemotron_asr"


def test_role_target_whisper_keeps_whisper(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="whisper")
    assert server_audio_asr._local_asr().id == "whisper"


def test_without_a_role_target_foundry_comes_before_whisper_cpp(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"])
    assert server_audio_asr._local_asr().id == "nemotron_asr"


def test_explicit_whisper_is_still_served_beside_nemotron(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="nemotron_asr")
    assert server_audio_asr._whisper_model_for_request("whisper").id == "whisper"


def test_role_target_not_enabled_falls_back_to_the_enabled_asr(write_config) -> None:
    _config(write_config, enabled=["whisper"], role="nemotron_asr")
    assert server_audio_asr._local_asr().id == "whisper"


# An alias standing in for the runtime model: what a migration did by hand, which
# made /v1/models list Nemotron while whisper answered.
_ALIAS_FOR_RUNTIME = {**NEMOTRON, "virtual": True, "port": 18022}


def test_alias_for_a_runtime_model_is_refused_not_served(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="nemotron_asr",
            nemotron=_ALIAS_FOR_RUNTIME)
    assert "nemotron_asr" in model_registry.registry_problems()
    assert [m.id for m in model_registry.enabled_models()] == ["whisper"]
    with pytest.raises(HTTPException) as exc:
        server_audio_asr._local_asr()
    assert exc.value.status_code == 503 and "refused" in exc.value.detail


def test_foundry_row_needs_an_exact_variant(write_config) -> None:
    _config(write_config, enabled=["nemotron_asr"],
            nemotron={**NEMOTRON, "runtime_model": "nemotron-3.5-asr-streaming-0.6b"})
    assert "name:version" in model_registry.registry_problems()["nemotron_asr"]


def test_audio_health_reports_engine_and_exact_model(write_config, monkeypatch) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="nemotron_asr")
    from src.process import backend_process as bp
    from src.process import server_process as sp

    monkeypatch.setattr(bp, "is_reachable", lambda m, timeout=1.0: True)
    monkeypatch.setattr(sp, "snapshot_listening_pids", lambda: {})
    monkeypatch.setattr(sp, "snapshot_is_known", lambda snap: False)
    resp = server_audio_asr.audio_health()
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["backends"][0].items() >= {"id": "nemotron_asr", "engine": "foundry-local", "model": ASR_VARIANT}.items()


def test_audio_health_names_a_refused_role_target(write_config) -> None:
    _config(write_config, enabled=["whisper", "nemotron_asr"], role="nemotron_asr",
            nemotron=_ALIAS_FOR_RUNTIME)
    resp = server_audio_asr.audio_health()
    body = json.loads(resp.body)
    assert (resp.status_code, body["status"]) == (503, "refused")
    assert "alias" in body["detail"]
