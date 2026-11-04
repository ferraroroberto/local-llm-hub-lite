"""`scripts/comment_ratchet.py` -- the vendored structural check for issue-number
references in source comments.

The script is vendored byte-for-byte from project-scaffolding, which owns the
tests of its scanner and baseline modes; this repo pins only what is its own:
the ratchet is enabled in `.fleet.toml`, its baseline exists, and a git failure
is reported as not confirmed. No network, no model, no live backend.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import comment_ratchet as cr

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("line", "expect_hit"),
    [
        ("# fixed in issue #412, drop it", True),
        ("# see home-automation#118 for the lesson", True),
        ("--ink: #1f2328; /* a hex color, not an issue */", False),
        ("return ({ 'x': '&#39;' }[c]);  // HTML entity, not an issue", False),
        ("<use href=\"#i-wrench\"></use>", False),
        ("# no reference here at all", False),
    ],
)
def test_ref_regex_matches_issue_numbers_not_hex_or_entities(line: str, expect_hit: bool) -> None:
    assert bool(cr._REF.search(line)) is expect_hit


def test_git_failure_is_not_confirmed_not_a_pass_or_skip(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(cr.NotConfirmed):
        cr.tracked_files(not_a_repo)


def test_the_ratchet_is_declared_and_its_baseline_exists() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["comment_ratchet"]["enabled"] is True
    baseline = REPO_ROOT / fleet["comment_ratchet"]["baseline"]
    assert baseline.is_file()
