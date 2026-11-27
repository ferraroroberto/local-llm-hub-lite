"""The Foundry model cache folder (config key, env override, default) and the alias refusal, SDK faked."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from src import host_profile
from src.foundry import runtime, worker
from src.model_registry import ENGINE_FOUNDRY, Model
from tests.foundry import _fake_sdk as fake
from tests.foundry._fake_sdk import ASR_VARIANT, FakeCatalog, FakeVariant

ENV_VAR = "LOCAL_LLM_HUB_LITE_FOUNDRY_CACHE_DIR"
ASR_ROW = Model(id="nemotron_asr", display_name="nemotron-3.5-asr", backend="whisper",
                engine=ENGINE_FOUNDRY, port=18090, runtime_model=ASR_VARIANT)


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)


def _initialised_cache_dir(monkeypatch) -> Optional[str]:
    """The ``model_cache_dir`` the SDK is initialised with for the active config."""
    sdk = fake.install(monkeypatch, FakeCatalog({ASR_VARIANT: FakeVariant(ASR_VARIANT)}))
    runtime.open_catalog()
    assert sdk.FoundryLocalManager.initialized_with == [runtime.APP_NAME]
    return sdk.FoundryLocalManager.cache_dirs[0]


def _health_cache(variant_id: str = ASR_VARIANT) -> dict:
    body = TestClient(worker.build_app(ASR_ROW, FakeVariant(variant_id, is_loaded=True))).get("/health").json()
    assert body["model"] == ASR_VARIANT
    return body["model_cache"]


def test_config_key_in_the_overlay_sets_the_sdk_cache(monkeypatch, write_config, tmp_path) -> None:
    cache = tmp_path / "models-cache"
    cache.mkdir()
    write_config({"hosts": {"local": {"enabled": []}}})
    (tmp_path / host_profile.LOCAL_OVERLAY_NAME).write_text(
        f"foundry:\n  model_cache_dir: '{cache.as_posix()}'\n", encoding="utf-8"
    )

    assert _initialised_cache_dir(monkeypatch) == str(cache)
    assert _health_cache() == {"path": str(cache), "source": "config", "state": "present"}


def test_env_override_wins_over_the_config_key(monkeypatch, write_config, tmp_path) -> None:
    write_config({"foundry": {"model_cache_dir": str(tmp_path / "from-config")}})
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "from-env"))

    assert _initialised_cache_dir(monkeypatch) == str(tmp_path / "from-env")
    assert _health_cache() == {"path": str(tmp_path / "from-env"), "source": "env", "state": "missing"}


@pytest.mark.parametrize("foundry", [None, {}, {"model_cache_dir": None}, {"model_cache_dir": "  "}])
def test_without_a_key_the_sdk_keeps_its_own_default(monkeypatch, write_config, foundry) -> None:
    write_config({"foundry": foundry})
    assert _initialised_cache_dir(monkeypatch) is None


def test_default_cache_is_reported_as_lite_own_folder(monkeypatch, write_config, tmp_path) -> None:
    write_config({})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected = tmp_path / f".{runtime.APP_NAME}" / "cache" / "models"
    assert _health_cache() == {"path": str(expected), "source": "default", "state": "missing"}


def test_a_relative_key_is_read_from_the_repo_root(monkeypatch, write_config) -> None:
    write_config({"foundry": {"model_cache_dir": "models/foundry"}})
    assert _initialised_cache_dir(monkeypatch) == str(host_profile.PROJECT_ROOT / "models" / "foundry")


@pytest.mark.parametrize("foundry, message", [
    ("not-a-mapping", "must be a mapping"),
    ({"model_cache_dir": 3}, "must be a folder path"),
    ({"model_cache_dir": __file__}, "is a file"),
])
def test_an_unusable_key_is_refused(monkeypatch, write_config, foundry, message: str) -> None:
    write_config({"foundry": foundry})
    with pytest.raises(runtime.FoundryError, match=message):
        _initialised_cache_dir(monkeypatch)


def test_an_unreadable_cache_folder_is_unknown_not_missing(monkeypatch, write_config, tmp_path) -> None:
    write_config({"foundry": {"model_cache_dir": str(tmp_path)}})

    def _denied(self, *args, **kwargs):
        raise PermissionError("denied")

    client = TestClient(worker.build_app(ASR_ROW, FakeVariant(ASR_VARIANT, is_loaded=True)))
    monkeypatch.setattr(Path, "stat", _denied)
    assert client.get("/health").json()["model_cache"]["state"] == "unknown"


def test_an_alias_is_refused_naming_it_and_the_variant_it_resolves_to(monkeypatch) -> None:
    other = "nemotron-3.5-asr-streaming-0.6b-generic-gpu:1"
    fake.install(monkeypatch, FakeCatalog({other: FakeVariant(other)}))
    alias = "nemotron-3.5-asr-streaming-0.6b-generic-gpu"
    with pytest.raises(runtime.FoundryError) as refused:
        runtime.resolve_variant(runtime.open_catalog(), alias)
    assert repr(alias) in str(refused.value) and repr(other) in str(refused.value)


def test_another_variant_id_is_refused_naming_both(monkeypatch) -> None:
    other = "nemotron-3.5-asr-streaming-0.6b-generic-cpu:2"
    fake.install(monkeypatch, FakeCatalog({other: FakeVariant(other)}, redirect={ASR_VARIANT: other}))
    with pytest.raises(runtime.FoundryError) as refused:
        runtime.resolve_variant(runtime.open_catalog(), ASR_VARIANT)
    assert repr(ASR_VARIANT) in str(refused.value) and repr(other) in str(refused.value)
