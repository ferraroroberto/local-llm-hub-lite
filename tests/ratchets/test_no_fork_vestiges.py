"""Fail when wording from features this lite fork removed reappears.

Multi-host routing, the Cloudflare surface and fleet satellites live in the
upstream full fork; copied code or docs keep dragging their vocabulary back in
and misleading readers about what this fork does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

# Phrases only the removed upstream features use. `cloudflared`-style header
# names in auth_middleware are a live concern and deliberately not matched.
VESTIGES = re.compile(
    r"satellite|another host|remote-owned|mac-mini|gaming's"
    r"|cloudflare tunnel|copy cloudflare url",
    re.IGNORECASE,
)

SKIPPED_PREFIXES = ("app_web/static/_vendored/", "vendor/", "models/")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True,
        check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return [
        REPO / rel for rel in out.split("\0")
        if rel and not rel.startswith(SKIPPED_PREFIXES)
    ]


def test_no_removed_feature_vocabulary_in_tracked_text():
    hits: list[str] = []
    for path in _tracked_files():
        if path.resolve() == THIS_FILE or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary asset
        for lineno, line in enumerate(text.splitlines(), start=1):
            if VESTIGES.search(line):
                hits.append(f"{path.relative_to(REPO).as_posix()}:{lineno}: {line.strip()}")
    assert not hits, "removed-feature vocabulary found:\n" + "\n".join(hits)
