"""Host-profile resolution, including the lite-fork cross-platform fallback.

#51: ``resolve()`` only matched a ``hosts:`` row by ``hostname`` or by
``platform == sys.platform``, and the shipped ``config/models.yaml`` has a
single ``platform: win32`` row. On any non-Windows box every caller of
``resolve()`` (``/v1/models``, ``/v1/messages``, ``/v1/chat/completions``,
the admin Models tab, hub autostart) failed with
``RuntimeError: no host row matched platform=...`` even though
``run_hub.sh`` / ``launchers/*.sh`` and this repo's own CLAUDE.md advertise
the hub as cross-platform.
"""

from __future__ import annotations

from src import host_profile


def test_resolve_falls_back_to_sole_row_on_platform_mismatch(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {"local": {"platform": "win32", "default": True, "enabled": ["qwen"]}},
        "models": {"qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081}},
    })
    monkeypatch.delenv(host_profile.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "not-a-match")
    monkeypatch.setattr(host_profile.sys, "platform", "linux")

    prof = host_profile.resolve()
    assert prof.id == "local"


def test_resolve_falls_back_to_default_row_when_several_rows_mismatch(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True, "enabled": ["qwen"]},
            "mac": {"platform": "darwin", "enabled": ["qwen"]},
        },
        "models": {"qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081}},
    })
    monkeypatch.delenv(host_profile.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "not-a-match")
    monkeypatch.setattr(host_profile.sys, "platform", "linux")

    prof = host_profile.resolve()
    assert prof.id == "pc"


def test_resolve_still_raises_with_no_sole_row_and_no_default(monkeypatch, write_config):
    """Preserve the old loud failure when the fallback is genuinely
    ambiguous — more than one row, none marked ``default``."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "enabled": ["qwen"]},
            "mac": {"platform": "darwin", "enabled": ["qwen"]},
        },
        "models": {"qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081}},
    })
    monkeypatch.delenv(host_profile.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "not-a-match")
    monkeypatch.setattr(host_profile.sys, "platform", "linux")

    try:
        host_profile.resolve()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "no host row matched" in str(e)


def test_resolve_against_shipped_config_on_non_windows_platforms(monkeypatch):
    """Drive ``resolve()`` against the real, committed ``config/models.yaml``
    (not a synthetic fixture) under a forced non-Windows ``sys.platform`` —
    the exact repro in the finding."""
    monkeypatch.delenv(host_profile.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "not-a-match")
    host_profile._CONFIG_CACHE.clear()
    try:
        for forced_platform in ("linux", "darwin"):
            monkeypatch.setattr(host_profile.sys, "platform", forced_platform)
            prof = host_profile.resolve()
            assert prof.id == "local"
    finally:
        host_profile._CONFIG_CACHE.clear()
