"""Foundry Local runtime helper against a faked SDK: exact resolve, start or reuse, missing SDK."""

from __future__ import annotations

import pytest

from src.foundry import runtime
from tests.foundry import _fake_sdk as fake
from tests.foundry._fake_sdk import ASR_VARIANT, FakeCatalog, FakeVariant


def _catalog(monkeypatch, **variant_kw) -> FakeVariant:
    variant = FakeVariant(ASR_VARIANT, **variant_kw)
    fake.install(monkeypatch, FakeCatalog({ASR_VARIANT: variant}))
    return variant


def test_resolve_returns_the_exact_catalog_variant(monkeypatch) -> None:
    variant = _catalog(monkeypatch)
    assert runtime.resolve_variant(runtime.open_catalog(), ASR_VARIANT) is variant


def test_manager_initializes_once_under_the_hub_app_name(monkeypatch) -> None:
    _catalog(monkeypatch)
    runtime.open_catalog()
    runtime.open_catalog()
    import foundry_local_sdk

    assert foundry_local_sdk.FoundryLocalManager.initialized_with == [runtime.APP_NAME]


@pytest.mark.parametrize("name", ["nemotron-3.5-asr-streaming-0.6b", "", "unknown-model:1"])
def test_alias_or_unknown_name_is_refused(monkeypatch, name: str) -> None:
    _catalog(monkeypatch)
    with pytest.raises(runtime.FoundryError):
        runtime.resolve_variant(runtime.open_catalog(), name)


def test_catalog_handing_back_another_variant_is_refused(monkeypatch) -> None:
    other = "nemotron-3.5-asr-streaming-0.6b-generic-cpu:2"
    fake.install(monkeypatch, FakeCatalog({other: FakeVariant(other)}, redirect={ASR_VARIANT: other}))
    with pytest.raises(runtime.FoundryError, match="refusing a different variant"):
        runtime.resolve_variant(runtime.open_catalog(), ASR_VARIANT)


def test_ensure_loaded_starts_then_reuses(monkeypatch) -> None:
    variant = _catalog(monkeypatch)
    assert runtime.ensure_loaded(ASR_VARIANT) == (variant, runtime.LOADED_STARTED)
    assert runtime.ensure_loaded(ASR_VARIANT) == (variant, runtime.LOADED_REUSED)
    assert variant.loads == 1


def test_ensure_loaded_downloads_a_missing_variant_first(monkeypatch) -> None:
    variant = _catalog(monkeypatch, is_cached=False)
    runtime.ensure_loaded(ASR_VARIANT)
    assert (variant.downloads, variant.loads) == (1, 1)


def test_ensure_loaded_without_download_refuses_an_uncached_variant(monkeypatch) -> None:
    variant = _catalog(monkeypatch, is_cached=False)
    with pytest.raises(runtime.FoundryError, match="not cached"):
        runtime.ensure_loaded(ASR_VARIANT, allow_download=False)
    assert variant.loads == 0


def test_missing_sdk_says_foundry_local_is_not_installed(monkeypatch) -> None:
    fake.uninstall(monkeypatch)
    monkeypatch.setattr(runtime, "sdk_version", lambda: None)
    with pytest.raises(runtime.FoundryError, match="Foundry Local not installed"):
        runtime.open_catalog()
    assert runtime.sdk_status() == ("missing", runtime.NOT_INSTALLED)


def test_sdk_off_the_pin_is_a_warning(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "sdk_version", lambda: "2.5.0")
    status, detail = runtime.sdk_status()
    assert status == "warn" and runtime.SDK_PIN in detail
