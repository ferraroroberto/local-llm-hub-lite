"""The tray's single-instance mutex is per hub port, so the lite tray runs beside the full hub's."""

from __future__ import annotations

from tray.__main__ import tray_mutex_name


def test_mutex_name_carries_the_default_hub_port():
    assert tray_mutex_name() == r"Global\local-llm-hub-lite-8020-tray"


def test_mutex_name_follows_a_configured_hub_port(write_config):
    write_config({"hub": {"port": 8123}, "hosts": {"local": {"enabled": []}}, "models": {}})

    assert tray_mutex_name() == r"Global\local-llm-hub-lite-8123-tray"


def test_mutex_name_differs_from_the_full_hubs_tray():
    assert tray_mutex_name() != r"Global\local-llm-hub-tray"
