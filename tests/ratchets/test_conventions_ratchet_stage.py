"""The gate's conventions-ratchet stage fails on exit 3 as NOT CONFIRMED.

`scripts/conventions_ratchet.py` exits 3 when it could not establish the facts
(git unusable, a tracked .py file that does not parse). The gate calls it
directly and its `Step` helper fails on any non-zero exit, so 3 stops the gate
like a finding does. These tests pin both halves: the stage's wiring in
scripts/verify-before-ship.ps1, and the script's exit 3 on an unparseable file.
They use a throwaway git repo: no network and no live backend.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
SCRIPT = REPO_ROOT / "scripts" / "conventions_ratchet.py"


def _stage_body(label: str) -> str:
    start = GATE.index(f'Step "{label}" {{')
    return GATE[start : GATE.index("\n}\n", start)]


def test_stage_runs_the_script_directly_after_dead_code_and_before_pytest() -> None:
    assert (
        GATE.index('Step "dead code ratchet"')
        < GATE.index('Step "conventions ratchet"')
        < GATE.index('Step "pytest (unit)"')
    )
    body = _stage_body("conventions ratchet")
    assert r"& $Python scripts\conventions_ratchet.py" in body
    assert "||" not in body and "$null" not in body  # nothing folds a failure


def test_step_fails_on_any_nonzero_exit() -> None:
    step = GATE[GATE.index("function Step") : GATE.index("# Ask the router")]
    assert "$LASTEXITCODE -ne 0" in step
    assert "exit $LASTEXITCODE" in step


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_unparseable_tracked_file_exits_3_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / ".fleet.toml").write_text(
        '[conventions_ratchet]\nenabled = true\nrules = ["no-window"]\n',
        encoding="utf-8",
    )
    (tmp_path / ".conventions-ratchet-baseline.json").write_text(
        '{"version": 1, "entries": []}\n', encoding="utf-8"
    )
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "broken.py" in proc.stdout + proc.stderr
