r"""The gate's comment-ratchet stage: run the script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate

`scripts/comment_ratchet.py` (repo-local, not vendored) exits 0 on a pass, 1
on a new/grown issue-number reference or a missing baseline, 2 on a bad
baseline/arguments and 3 when it could not confirm (git unusable, a non-UTF-8
file). Exit 3 means part of the tree was not read, so it fails the gate as
its own state and never reads as a pass or a skip. This wrapper is the one
place that mapping lives, so a test can pin it; `verify-before-ship.ps1`
calls the wrapper, never the script directly. The runner itself is shared
with the static-ratchet / docs-ratchet stages.

Exit 0 only when the script exited 0; every other outcome exits 1 with its
reason on the last line.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG, EXIT_NEW, EXIT_NOT_CONFIRMED, EXIT_PASS, run_stage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "comment_ratchet.py"

_FAIL_LABELS = {
    EXIT_NEW: "NEW issue-number reference in a source comment, or missing baseline",
    EXIT_CONFIG: "bad baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be read, so it was not checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no in-scope source comment carries a new issue-number reference"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """The comment-ratchet stage: the shared runner with this stage's verdicts."""
    return run_stage(script, "comment ratchet", classify_exit)


if __name__ == "__main__":
    sys.exit(run())
