"""The gate's comment-ratchet stage maps the script's exit codes.

The runner (timeout, unstartable script, venv interpreter) is shared with
the static-ratchet stage and pinned in test_static_ratchet_gate.py.

`scripts/comment_ratchet.py` exits 3 when it could not confirm (git unusable,
a non-UTF-8 file): part of the tree was not read. These tests pin that 3
fails as NOT CONFIRMED -- never a pass, never a skip -- and that the pre-ship
gate calls the wrapper that guarantees it rather than the script. They run
fake scripts in a temp directory: no model and no live backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import comment_ratchet_gate as gate
from tests.ratchets.test_static_ratchet_gate import (
    _fake_script,
    assert_only_zero_passes_and_three_is_not_confirmed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_only_exit_zero_passes_and_exit_three_is_never_a_skip() -> None:
    assert_only_zero_passes_and_three_is_not_confirmed(gate.classify_exit)


@pytest.mark.parametrize("code", [1, 2, 3])
def test_run_wires_this_stages_verdict_into_the_shared_runner(
    code: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The runner mechanics (echoing, timeout/unstartable handling) are pinned
    # once in test_static_ratchet_gate.py; this only checks the stage name and
    # this module's own classify_exit land in the printed verdict line.
    gate.run(_fake_script(tmp_path, code, "src/x.py: not valid UTF-8"))
    verdict = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert verdict.startswith("FAIL: comment ratchet:")


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(
        encoding="utf-8"
    )
    lines = gate_ps1.splitlines()
    stage = [i for i, line in enumerate(lines) if line.startswith('Step "comment ratchet"')]
    assert len(stage) == 1
    body = lines[stage[0] + 1 : lines.index("}", stage[0])]
    calls = [line for line in body if line.strip().startswith("&")]
    assert len(calls) == 1
    assert "-m scripts.comment_ratchet_gate" in calls[0]
    assert "comment_ratchet.py" not in calls[0]
    # After the docs ratchet, before the unit suite.
    docs_at = next(i for i, line in enumerate(lines) if line.startswith('Step "docs ratchet"'))
    pytest_at = next(i for i, line in enumerate(lines) if line.startswith('Step "pytest (unit)"'))
    assert docs_at < stage[0] < pytest_at


def test_the_ratchet_script_is_repo_local_not_vendored() -> None:
    """Unlike static/docs ratchet, this check has no project-scaffolding
    equivalent -- it must not be declared in .fleet.toml's [vendored] table."""
    import tomllib

    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert "comment_ratchet" not in fleet.get("vendored", {})
