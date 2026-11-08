"""Runtime guard for write destinations that a caller, config value or CLI argument names.

The runtime-write ratchet resolves a write target statically; a path that arrives as a
parameter cannot be followed. ``checked_write_path`` closes that gap at run time: it
returns the path unchanged unless it resolves under one of the repo's code folders, in
which case it raises. It is listed in ``.fleet.toml`` ``[runtime_write_ratchet] safe_calls``,
so a write through it needs no per-call marker.

Gitignored live state inside the checkout (``config/``, ``data/``, ``webapp/``,
``vendor/``, ``models/``) stays allowed; only the folders that hold code are refused.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

#: Mirrors ``[runtime_write_ratchet] code_folders`` in ``.fleet.toml``; a test keeps them equal.
CODE_FOLDERS = ("src", "app_web", "tray", "scripts")

_REPO_ROOT = Path(__file__).resolve().parents[1]


class CodeFolderWriteRefused(ValueError):
    """A write destination resolved under a code folder."""


def _norm(path: Path) -> Path:
    return Path(os.path.normcase(str(path)))


def checked_write_path(path: Union[str, "os.PathLike[str]"]) -> Path:
    """Return ``path`` as a ``Path``; raise if it resolves under a code folder."""
    candidate = Path(path)
    resolved = _norm(candidate.resolve())
    for folder in CODE_FOLDERS:
        if resolved.is_relative_to(_norm(_REPO_ROOT / folder)):
            raise CodeFolderWriteRefused(
                f"refusing to write under the code folder {folder!r}: {candidate}"
            )
    return candidate
