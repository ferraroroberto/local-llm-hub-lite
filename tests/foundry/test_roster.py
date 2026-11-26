"""The migrated Nemotron roster: shipped defaults, the opt-in overlay, and the install row."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src import host_profile, install, model_registry
from src.foundry import runtime
from src.model_registry import ENGINE_FOUNDRY, STARTUP_EAGER
from tests.foundry._fake_sdk import ASR_VARIANT

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# What the other PC's working install served, as the issue records it.
MIGRATED = {
    "enabled": ["qwen35_4b", "nemotron4b", "nemotron_asr", "whisper"],
    "transcribe_role": "nemotron_asr",
    "aliases": {
        "qwen35_4b": ["agentic_light", "agentic_heavy"],
        "nemotron4b": [],
        "nemotron_asr": ["audio_transcribe"],
        "whisper": [],
    },
    "engines": {
        "qwen35_4b": "llama-server",
        "nemotron4b": "llama-server",
        "nemotron_asr": ENGINE_FOUNDRY,
        "whisper": "whisper-server",
    },
    "runtime_model": ASR_VARIANT,
    "nemotron_asr_startup": STARTUP_EAGER,
}


@pytest.fixture
def scratch_config(tmp_path, monkeypatch):
    """The shipped registry copied to a scratch dir; returns that dir's overlay path."""
    shutil.copyfile(CONFIG_DIR / "models.yaml", tmp_path / "models.yaml")
    monkeypatch.setattr(host_profile, "CONFIG_PATH", tmp_path / "models.yaml")
    host_profile._CONFIG_CACHE.clear()
    return tmp_path / host_profile.LOCAL_OVERLAY_NAME


def _roster() -> dict:
    enabled = model_registry.enabled_models()
    return {
        "enabled": host_profile.resolve().enabled,
        "transcribe_role": model_registry.transcribe_role_target(),
        "aliases": {m.id: m.aliases for m in enabled},
        "engines": {m.id: m.engine for m in enabled},
        "runtime_model": next(m.runtime_model for m in enabled if m.id == "nemotron_asr"),
        "nemotron_asr_startup": next(m.startup for m in enabled if m.id == "nemotron_asr"),
    }


def test_shipped_defaults_stay_qwen_and_whisper(scratch_config) -> None:
    assert host_profile.resolve().enabled == ["qwen35_4b", "whisper"]
    assert model_registry.transcribe_role_target() == "whisper"
    assert model_registry.registry_problems() == {}


def test_foundry_overlay_reproduces_the_migrated_roster(scratch_config) -> None:
    shutil.copyfile(CONFIG_DIR / "models.foundry.yaml", scratch_config)
    host_profile._CONFIG_CACHE.clear()
    assert _roster() == MIGRATED
    assert model_registry.registry_problems() == {}
    # Every name /v1/models lists resolves to the row that runs it.
    names = [n for m in model_registry.enabled_models() for n in m.all_names]
    assert len(names) == len(set(names))


def test_overlay_merges_mappings_and_replaces_lists() -> None:
    base = {"hosts": {"local": {"enabled": ["a"], "vram_mb": 4096}}, "models": {"a": {"port": 1, "args": ["x"]}}}
    overlay = {"hosts": {"local": {"enabled": ["a", "b"]}}, "models": {"a": {"args": []}}}
    assert host_profile.merge_overlay(base, overlay) == {
        "hosts": {"local": {"enabled": ["a", "b"], "vram_mb": 4096}},
        "models": {"a": {"port": 1, "args": []}},
    }


def test_install_row_without_foundry_says_it_is_not_installed(scratch_config, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "sdk_version", lambda: None)
    [row] = install._check_foundry()
    assert row.status == "warn" and "Foundry Local not installed" in row.detail


def test_install_row_with_the_roster_and_no_sdk_is_missing_with_a_fix(scratch_config, monkeypatch) -> None:
    shutil.copyfile(CONFIG_DIR / "models.foundry.yaml", scratch_config)
    host_profile._CONFIG_CACHE.clear()
    monkeypatch.setattr(runtime, "sdk_version", lambda: None)
    [row] = install._check_foundry()
    assert (row.status, row.fix_id) == ("missing", "foundry")
    assert install.fix_fn_for(row) is install._fix_foundry
