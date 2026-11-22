"""Host-profile resolution: the single ``hosts:`` row is the host, on any platform."""

from __future__ import annotations

import pytest

from src import host_profile


def test_resolve_returns_the_sole_row(write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {"local": {"enabled": ["qwen"], "vram_mb": 8192}},
        "models": {"qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081}},
    })

    prof = host_profile.resolve()
    assert prof.id == "local"
    assert prof.enabled == ["qwen"]
    assert prof.vram_mb == 8192


def test_resolve_raises_when_no_hosts_defined(write_config):
    write_config({"hub": {"port": 8000}, "models": {}})

    with pytest.raises(RuntimeError, match="no 'hosts' defined"):
        host_profile.resolve()


def test_resolve_against_shipped_config():
    """Drive ``resolve()`` against the real, committed ``config/models.yaml``."""
    host_profile._CONFIG_CACHE.clear()
    try:
        prof = host_profile.resolve()
        assert prof.id == "local"
        assert prof.enabled
    finally:
        host_profile._CONFIG_CACHE.clear()


def test_hub_default_port_is_not_the_full_hubs_8000():
    """The lite hub sits beside the full hub (:8000), so its default is its own port."""
    assert host_profile.DEFAULT_HUB_PORT == 8020


def test_hub_port_falls_back_to_the_default_when_config_names_none(write_config):
    write_config({"hosts": {"local": {"enabled": []}}, "models": {}})

    assert host_profile.hub_port() == host_profile.DEFAULT_HUB_PORT


def test_shipped_config_binds_the_default_hub_port():
    """The committed registry and the code default agree, and sit clear of the full hub."""
    host_profile._CONFIG_CACHE.clear()
    try:
        assert host_profile.hub_port() == host_profile.DEFAULT_HUB_PORT
        assert host_profile.hub_port() != 8000
    finally:
        host_profile._CONFIG_CACHE.clear()
