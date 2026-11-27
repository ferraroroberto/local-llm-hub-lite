"""The gate's port-host and clean-tree stages, and the two seams they rest on.

Both ratchets exit 3 when they could not establish the facts; the gate calls each
script directly and its `Step` helper fails on any non-zero exit, so 3 stops the
gate like a finding does. These tests pin that wiring, the resolver the `.bat`
launchers ask for the hub port, and the scratch config the clean-tree smoke boots
from. No network, no live backend.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")


def _stage_body(label: str) -> str:
    start = GATE.index(f'Step "{label}" {{')
    return GATE[start : GATE.index("\n}\n", start)]


def test_stages_run_the_scripts_directly_between_windows_only_and_the_probe() -> None:
    assert (
        GATE.index('Step "windows-only ratchet"')
        < GATE.index('Step "port host ratchet"')
        < GATE.index('Step "clean tree ratchet"')
        < GATE.index('Step "runtime write probe (snapshot)"')
    )
    for label, script in (
        ("port host ratchet", r"scripts\port_host_ratchet.py"),
        ("clean tree ratchet", r"scripts\clean_tree_ratchet.py"),
    ):
        body = _stage_body(label)
        assert f"& $Python {script}" in body
        assert "||" not in body and "$null" not in body  # nothing folds a failure


def test_install_standard_stage_runs_the_script_directly_after_port_host() -> None:
    assert (
        GATE.index('Step "port host ratchet"')
        < GATE.index('Step "install standard ratchet"')
        < GATE.index('Step "clean tree ratchet"')
    )
    body = _stage_body("install standard ratchet")
    assert r"& $Python scripts\install_standard_ratchet.py" in body
    assert "||" not in body and "$null" not in body


def test_host_profile_prints_the_configured_hub_port() -> None:
    from src.host_profile import hub_port

    out = subprocess.run(
        [sys.executable, "-m", "src.host_profile"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.stdout.strip() == str(hub_port())


@pytest.mark.parametrize("launcher", ["tray.bat", "run_hub.bat"])
def test_launchers_read_the_port_from_the_resolver(launcher: str) -> None:
    text = (REPO_ROOT / launcher).read_text(encoding="utf-8")
    assert "-m src.host_profile" in text
    assert "%HUB_PORT%" in text


def test_smoke_hub_boots_from_a_scratch_config_with_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import smoke_hub
    from src import host_profile, server

    served: list[tuple[int, list[str]]] = []

    def _fake_main() -> None:
        served.append((host_profile.hub_port(), host_profile.resolve().enabled))

    monkeypatch.setattr(host_profile, "CONFIG_PATH", host_profile.CONFIG_PATH)
    monkeypatch.setattr(server, "main", _fake_main)

    assert smoke_hub.main(["18999", str(tmp_path)]) == 0

    # The scratch config names the smoke's port and enables no model, so the hub
    # boot starts no backend and never reads the live port.
    assert served == [(18999, [])]
    assert yaml.safe_load((tmp_path / "models.yaml").read_text(encoding="utf-8"))["hosts"] == {
        "local": {"enabled": []}
    }


def test_smoke_hub_refuses_a_missing_argument() -> None:
    from scripts import smoke_hub

    assert smoke_hub.main(["18999"]) == 2
