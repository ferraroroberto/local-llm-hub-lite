"""``python -m src.audio.bench run --clips DIR`` and ``python -m src.audio.bench fetch``.

``run`` measures every installed candidate (or those ``--only`` names, with
``*`` wildcards) on the clips and writes the table to stdout and ``--out``.
``list`` shows each candidate and whether it is installed here.
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import sys
from pathlib import Path
from typing import List, Optional

from ...runtime_files import checked_write_path
from . import candidates as cands
from .clips import ClipError, load_clips
from .fetch import fetch, items
from .measure import GpuProbe, run_all
from .report import render, transcripts

logger = logging.getLogger(__name__)


def _selected(root: Path, patterns: Optional[List[str]]) -> List[cands.Candidate]:
    every = cands.catalog(root)
    if not patterns:
        return every
    return [c for c in every if any(fnmatch.fnmatch(c.id, p) for p in patterns)]


def _write(path: Optional[Path], text: str) -> None:
    if path:
        target = checked_write_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        logger.info("✅ wrote %s", target)


def _run(args: argparse.Namespace) -> int:
    try:
        clips = load_clips(args.clips)
    except ClipError as exc:
        logger.error("❌ %s", exc)
        return 2
    selected = _selected(args.root, args.only)
    if not selected:
        logger.error("❌ no candidate matches %s", args.only)
        return 2
    gpu = GpuProbe(args.gpu_index)
    rows = run_all(selected, clips, avoid=cands.registry_ports(), language=args.language, gpu=gpu)
    table = render(rows, clips, gpu.name())
    sys.stdout.write(table)
    _write(args.out, table)
    _write(args.transcripts, transcripts(rows, clips))
    return 0


def _list(args: argparse.Namespace) -> int:
    for c in _selected(args.root, args.only):
        try:
            reason = c.missing()
        except Exception as exc:  # noqa: BLE001 — one broken install must not hide the others
            reason = f"install check failed: {type(exc).__name__}: {exc}"
        sys.stdout.write(f"{c.id:36} {'installed' if reason is None else 'not installed: ' + reason}\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m src.audio.bench")
    parser.add_argument("--root", type=Path, default=cands.PROJECT_ROOT,
                        help="checkout whose vendor/ and models/ hold the engines (default: this one)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="measure the installed candidates")
    run.add_argument("--clips", type=Path, required=True,
                     help="folder of 16 kHz mono 16-bit WAVs, each with an optional same-stem .txt reference")
    run.add_argument("--only", nargs="+", help="candidate ids or patterns (see `list`)")
    run.add_argument("--language", default="en")
    run.add_argument("--gpu-index", type=int, default=0)
    run.add_argument("--out", type=Path, help="also write the table here")
    run.add_argument("--transcripts", type=Path, help="write each candidate's text per clip here")
    lst = sub.add_parser("list", help="show each candidate and whether it is installed")
    lst.add_argument("--only", nargs="+")
    get = sub.add_parser("fetch", help="download the benchmark-only models")
    get.add_argument("items", nargs="*", help=f"any of: {', '.join(items())} (default: all)")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "list":
        return _list(args)
    return fetch(args.root, args.items)


if __name__ == "__main__":
    sys.exit(main())
