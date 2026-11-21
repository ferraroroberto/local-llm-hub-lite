"""Fail when a second home for the ``CREATE_NO_WINDOW`` flag appears.

``src/no_window.py`` is the one definition. Only a vendor-verbatim file (a
``.fleet.toml`` ``[vendored]`` dest) may derive the flag itself, because its
bytes must stay self-contained in every adopter repo.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

# A local re-derivation of the flag, or a helper wrapping one.
SECOND_HOME = re.compile(
    r"CREATE_NO_WINDOW\s+if\s+sys\.platform|def\s+no_window_flags\b"
)


def _vendored_dests() -> tuple[str, ...]:
    manifest = tomllib.loads((REPO / ".fleet.toml").read_text(encoding="utf-8"))
    return tuple(entry["dest"] for entry in manifest.get("vendored", {}).values())


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True,
        check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return [REPO / line for line in out.stdout.splitlines() if line]


def test_only_vendored_files_derive_the_no_window_flag() -> None:
    vendored = _vendored_dests()
    offenders = []
    for path in _tracked_python_files():
        if path.resolve() == THIS_FILE or not path.is_file():
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(vendored):
            continue
        if SECOND_HOME.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "import NO_WINDOW from src.no_window instead of re-deriving it: "
        + ", ".join(offenders)
    )
