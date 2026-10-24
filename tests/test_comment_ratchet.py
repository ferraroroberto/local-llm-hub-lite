"""`scripts/comment_ratchet.py` -- the structural check for audit#53
(claude-md-drift: issue-number narratives in source comments).

Exercises the scanner and its baseline modes directly against a throwaway
git repo in a temp directory: no network, no model, no live backend.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import comment_ratchet as cr
from src.no_window import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=NO_WINDOW)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
                   creationflags=NO_WINDOW)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True,
                   creationflags=NO_WINDOW)
    return tmp_path


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=root, check=True, creationflags=NO_WINDOW)


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


def test_scan_counts_refs_per_file_and_skips_vendored_marker(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _write(root, "src/a.py", "# why this exists (issue #1)\nx = 1\n")
    _write(
        root,
        "tray/single_instance.py",
        '"""CANONICAL, VENDORED VERBATIM from project-scaffolding (#39)."""\nx = 1\n',
    )
    _write(root, "tests/not_in_scope.py", "# issue #2 — outside the scanned dirs\n")
    found = cr.scan(root)
    assert found == {"src/a.py": 1}


def test_scan_respects_fleet_toml_vendored_dests(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _write(
        root,
        ".fleet.toml",
        '[vendored]\nthing = { src = "x", sha = "y", dest = "scripts/vendored_thing.py" }\n',
    )
    _write(root, "scripts/vendored_thing.py", "# upstream issue #7\n")
    _write(root, "scripts/real.py", "# upstream issue #8\n")
    found = cr.scan(root)
    assert found == {"scripts/real.py": 1}


def test_check_fails_without_a_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _init_repo(tmp_path)
    _write(root, "src/a.py", "x = 1\n")
    code = cr.check(root, root / ".comment-ratchet-baseline.json")
    assert code == cr.EXIT_NEW
    assert "no baseline" in capsys.readouterr().out


def test_check_passes_at_baseline_and_fails_above_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _init_repo(tmp_path)
    baseline = root / ".comment-ratchet-baseline.json"
    _write(root, "src/a.py", "# issue #1\nx = 1\n")
    assert cr.init(root, baseline) == cr.EXIT_PASS
    assert cr.check(root, baseline) == cr.EXIT_PASS

    # A second, NEW reference in the same file rises above the baseline.
    _write(root, "src/a.py", "# issue #1\n# issue #2\nx = 1\n")
    code = cr.check(root, baseline)
    out = capsys.readouterr().out
    assert code == cr.EXIT_NEW
    assert "src/a.py: 2 reference(s), baseline allows 1" in out


def test_update_baseline_refuses_growth_without_allow_additions(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    baseline = root / ".comment-ratchet-baseline.json"
    _write(root, "src/a.py", "x = 1\n")
    assert cr.init(root, baseline) == cr.EXIT_PASS

    _write(root, "src/a.py", "# issue #1\nx = 1\n")
    assert cr.update_baseline(root, baseline, allow_additions=False) == cr.EXIT_CONFIG
    assert cr.update_baseline(root, baseline, allow_additions=True) == cr.EXIT_PASS
    assert cr.check(root, baseline) == cr.EXIT_PASS


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
