r"""Ratchet on issue-number narratives inside source-code comments.

Catches the claude-md-drift class the docs ratchet (`scripts/docs_ratchet.py`)
doesn't cover: upstream issue references (`#412`, `issue #198`,
`home-automation#118`, ...) baked into `src/`, `app_web/`, `scripts/`,
`tray/` and `launchers/` comments and docstrings, instead of the issue and
git log the global CLAUDE.md says decision history belongs in. A reader who
follows one of these numbers into *this* repo's tracker lands on nothing, or
on an unrelated issue -- audit#53's finding, repeated across two consecutive
audits.

Counts the same `#<digits>` reference shape the docs ratchet uses (hex
colors like `#1f2328` and HTML entities like `&#39;` are excluded by the same
lookaround), per file, against a committed baseline: it fails only when a
count *rises*. Existing debt never blocks work, but it cannot grow.

Scope: `git ls-files` restricted to `src/`, `app_web/`, `scripts/`, `tray/`,
`launchers/`, extensions `.py .js .css .html .bat .sh .ps1`, minus every
`dest` in this repo's `.fleet.toml` `[vendored]` table,
`app_web/static/_vendored/**`, and any file whose first 1 KiB carries a
per-file "whole file is vendored" header (`VENDORED COPY` or `VENDORED
VERBATIM` -- `scripts/tray_lifecycle.ps1` and `tray/single_instance.py`):
fixed upstream in project-scaffolding and re-propagated, never edited
per-app -- editing either here would fork it from the rest of the fleet
silently.

Modes:
  check             (default) compare the current scan to the baseline.
  --init            write the first baseline; refuses to overwrite one.
  --update-baseline rewrite the baseline from the current scan. Shrinking or
                    dropping an entry always succeeds; raising one needs
                    --allow-additions, which shows in review.

Exit codes (a state is never folded into a pass):
  0  pass: no in-scope file carries more references than its baseline entry
  1  a count rose beyond its baseline entry, a new file carries references
     with no baseline entry, or (check mode) the baseline file is missing
  2  bad baseline JSON, bad CLI usage, or --update-baseline tried to grow an
     entry without --allow-additions -- fail loud
  3  NOT CONFIRMED: `git ls-files` failed, or an in-scope file is not valid
     UTF-8 -- the gate fails on it, never a pass or a skip
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / ".comment-ratchet-baseline.json"
GIT_TIMEOUT_S = 120
# run_stage() (scripts/static_ratchet_gate.py) invokes this as a bare script,
# not `python -m`, so REPO_ROOT is not reliably importable here; mirror the
# vendored ratchets' own inline flag instead of importing src.no_window.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

EXIT_PASS, EXIT_NEW, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

SCOPE_DIRS = ("src/", "app_web/", "scripts/", "tray/", "launchers/")
SCOPE_EXTENSIONS = {".py", ".js", ".css", ".html", ".bat", ".sh", ".ps1"}
# Both per-file "this whole file is vendored" header conventions seen in this
# repo: tray/single_instance.py's "CANONICAL, VENDORED VERBATIM" and
# scripts/tray_lifecycle.ps1's "VENDORED COPY". Checked near the top only, so
# an ordinary mention of the app_web/static/_vendored/ *directory* elsewhere
# in a file (lowercase, a different word) never matches.
_VENDORED_MARKER = re.compile(r"VENDORED (COPY|VERBATIM)")

# Same shape + lookaround as docs_ratchet.py's `_REF`: `#123`, not preceded by
# `&`/`#`/`/` (entity, doubled hash, URL path) and not followed by a word
# character or `-` (so `#1f2328` -- a hex color -- never matches: the digit
# run stops at the first letter and the lookahead then rejects it).
_REF = re.compile(r"(?<![&#/])#(\d{1,5})(?![\w-])")


class NotConfirmed(RuntimeError):
    pass


def _vendored_dests(root: Path) -> set[str]:
    fleet_path = root / ".fleet.toml"
    if not fleet_path.is_file():
        return set()
    data = tomllib.loads(fleet_path.read_text(encoding="utf-8"))
    return {entry["dest"] for entry in data.get("vendored", {}).values()}


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked file."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, env=env,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git ls-files timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git ls-files exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def in_scope(path: str, vendored: set[str]) -> bool:
    if not path.startswith(SCOPE_DIRS):
        return False
    if Path(path).suffix not in SCOPE_EXTENSIONS:
        return False
    if path in vendored:
        return False
    if path.startswith("app_web/static/_vendored/"):
        return False
    return True


def count_file(root: Path, path: str) -> int:
    try:
        text = (root / path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: not valid UTF-8 ({exc})") from exc
    if _VENDORED_MARKER.search(text[:1024]):
        return 0
    return len(_REF.findall(text))


def scan(root: Path) -> dict[str, int]:
    """In-scope reference counts, one entry per file that has at least one."""
    vendored = _vendored_dests(root)
    found: dict[str, int] = {}
    for path in tracked_files(root):
        if not in_scope(path, vendored):
            continue
        n = count_file(root, path)
        if n:
            found[path] = n
    return found


def _load_baseline(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read baseline {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, int) for v in data.values()):
        raise ValueError(f"baseline {path} is not a flat {{path: count}} map")
    return data


def _write_baseline(path: Path, counts: dict[str, int]) -> None:
    path.write_text(
        json.dumps(dict(sorted(counts.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def check(root: Path, baseline_path: Path) -> int:
    if not baseline_path.is_file():
        print(f"FAIL: no baseline at {baseline_path} -- run --init")
        return EXIT_NEW
    try:
        baseline = _load_baseline(baseline_path)
    except ValueError as exc:
        print(f"CONFIG: {exc}")
        return EXIT_CONFIG
    try:
        current = scan(root)
    except NotConfirmed as exc:
        print(f"NOT CONFIRMED: {exc}")
        return EXIT_NOT_CONFIRMED
    grown = {
        path: (n, baseline.get(path, 0))
        for path, n in current.items()
        if n > baseline.get(path, 0)
    }
    if not grown:
        print("OK: no in-scope file carries more issue-number references than its baseline entry")
        return EXIT_PASS
    for path, (now, was) in sorted(grown.items()):
        print(f"NEW: {path}: {now} reference(s), baseline allows {was}")
    return EXIT_NEW


def init(root: Path, baseline_path: Path) -> int:
    if baseline_path.is_file():
        print(f"CONFIG: {baseline_path} already exists -- use --update-baseline")
        return EXIT_CONFIG
    try:
        counts = scan(root)
    except NotConfirmed as exc:
        print(f"NOT CONFIRMED: {exc}")
        return EXIT_NOT_CONFIRMED
    _write_baseline(baseline_path, counts)
    print(f"OK: wrote {baseline_path} ({len(counts)} file(s))")
    return EXIT_PASS


def update_baseline(root: Path, baseline_path: Path, allow_additions: bool) -> int:
    try:
        old = _load_baseline(baseline_path) if baseline_path.is_file() else {}
    except ValueError as exc:
        print(f"CONFIG: {exc}")
        return EXIT_CONFIG
    try:
        new = scan(root)
    except NotConfirmed as exc:
        print(f"NOT CONFIRMED: {exc}")
        return EXIT_NOT_CONFIRMED
    grown = {p: (n, old.get(p, 0)) for p, n in new.items() if n > old.get(p, 0)}
    if grown and not allow_additions:
        for path, (now, was) in sorted(grown.items()):
            print(f"CONFIG: {path}: {now} reference(s) would raise the baseline from {was} -- pass --allow-additions")
        return EXIT_CONFIG
    _write_baseline(baseline_path, new)
    print(f"OK: wrote {baseline_path} ({len(new)} file(s))")
    return EXIT_PASS


def main(argv: list[str]) -> int:
    mode = "check"
    baseline_path = DEFAULT_BASELINE
    allow_additions = False
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        mode, rest = rest[0], rest[1:]
    while rest:
        arg = rest.pop(0)
        if arg == "--init":
            mode = "init"
        elif arg == "--update-baseline":
            mode = "update-baseline"
        elif arg == "--allow-additions":
            allow_additions = True
        elif arg == "--baseline":
            if not rest:
                print("CONFIG: --baseline needs a path")
                return EXIT_CONFIG
            baseline_path = Path(rest.pop(0))
        else:
            print(f"CONFIG: unknown argument {arg!r}")
            return EXIT_CONFIG

    if mode == "check":
        return check(REPO_ROOT, baseline_path)
    if mode == "init":
        return init(REPO_ROOT, baseline_path)
    if mode == "update-baseline":
        return update_baseline(REPO_ROOT, baseline_path, allow_additions)
    print(f"CONFIG: unknown mode {mode!r}")
    return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
