"""Shared test configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a throwaway ``models.yaml`` and point the config readers at it.

    Four test modules each carried their own ``_write_config`` /
    ``_patch_config_path`` pair — two of them with a comment saying they
    mirror ``test_model_registry.py``'s (#470). This is that pair, once:
    dump ``content`` to a temp ``models.yaml``, repoint both modules'
    ``CONFIG_PATH`` (each ``_load_config()`` reads the module attribute at
    call time, so no reload is needed) and drop ``host_profile``'s parsed
    cache so the next read actually re-parses the new file.

    Pass ``dirpath`` to write a *second* config in the same test — the file
    name is fixed, so a distinct directory is what makes it a distinct file.
    """
    from src import host_profile, model_registry

    def _write(content: dict, *, dirpath=None) -> Path:
        cfg = Path(dirpath or tmp_path) / "models.yaml"
        cfg.write_text(yaml.safe_dump(content), encoding="utf-8")
        monkeypatch.setattr(host_profile, "CONFIG_PATH", cfg)
        monkeypatch.setattr(model_registry, "CONFIG_PATH", cfg, raising=False)
        host_profile._CONFIG_CACHE.clear()
        return cfg

    return _write


@pytest.fixture(autouse=True)
def _reset_shared_http_clients():
    """Reset the hub's shared httpx client singletons around every test.

    The hub reuses one pooled ``httpx.AsyncClient`` / ``httpx.Client`` across
    requests (issue #165) and caches it module-side. Tests that monkeypatch
    ``httpx.AsyncClient`` / ``httpx.Client`` to a fake need the cache cleared so
    ``get_async_client()`` / ``get_sync_client()`` reconstruct the patched class
    fresh, and so a real client built in one test never leaks into the next.
    """
    from src import http_client

    http_client._async_client = None
    http_client._sync_client = None
    yield
    http_client._async_client = None
    http_client._sync_client = None
