"""Download the benchmark-only model files: ``python -m src.audio.bench fetch [item ...]``.

Each item lands under the root's ``models/`` (whisper.cpp weights, beside the
shipped one) or ``models/bench/``. An item already there is skipped, and
nothing is ever deleted or overwritten. Python packages are not installed
here: docs/asr-benchmark.md gives those commands.
"""

from __future__ import annotations

import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ...runtime_files import checked_write_path
from .candidates import (
    BENCH_MODELS,
    FASTER_WHISPER_DIR,
    SHERPA_DIR,
    SHERPA_LATENCIES,
    WHISPER_WEIGHTS,
)

logger = logging.getLogger(__name__)

WHISPER_REPO = "ggerganov/whisper.cpp"
FASTER_WHISPER_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
SHERPA_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"
_DOWNLOAD_TIMEOUT_S = 60.0
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Item:
    name: str
    target: Path      # relative to the root; present means fetched


def items() -> Dict[str, Item]:
    out = {"whisper": Item("whisper", Path("models") / WHISPER_WEIGHTS["q5_0"])}
    for latency in SHERPA_LATENCIES:
        out[f"sherpa-{latency}"] = Item(f"sherpa-{latency}",
                                        BENCH_MODELS / SHERPA_DIR.format(latency=latency))
    out["faster-whisper"] = Item("faster-whisper", BENCH_MODELS / FASTER_WHISPER_DIR)
    return out


def _download(url: str, dest: Path) -> None:
    partial = checked_write_path(dest.with_name(dest.name + ".part"))
    partial.parent.mkdir(parents=True, exist_ok=True)
    logger.info("ℹ️ downloading %s", url)
    with httpx.stream("GET", url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
        resp.raise_for_status()
        with partial.open("wb") as fh:
            for chunk in resp.iter_bytes(_CHUNK):
                fh.write(chunk)
    partial.replace(dest)


def _fetch_whisper(root: Path) -> None:
    from huggingface_hub import hf_hub_download

    for name in WHISPER_WEIGHTS.values():
        if (root / "models" / name).is_file():
            logger.info("ℹ️ %s already there", name)
            continue
        hf_hub_download(WHISPER_REPO, name, local_dir=str(checked_write_path(root / "models")))
        logger.info("✅ %s", name)


def _fetch_sherpa(root: Path, item: Item) -> None:
    folder = root / item.target
    archive = checked_write_path(folder.with_name(folder.name + ".tar.bz2"))
    if not archive.is_file():
        _download(SHERPA_RELEASE.format(name=folder.name), archive)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(checked_write_path(folder.parent), filter="data")
    archive.unlink()
    logger.info("✅ %s", folder.name)


def _fetch_faster_whisper(root: Path, item: Item) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(FASTER_WHISPER_REPO, local_dir=str(checked_write_path(root / item.target)))
    logger.info("✅ %s", item.target.name)


def fetch(root: Path, names: Optional[List[str]] = None) -> int:
    """Fetch ``names`` (all items when empty); 0 when every one is there afterwards."""
    catalog = items()
    unknown = [n for n in names or [] if n not in catalog]
    if unknown:
        logger.error("❌ unknown item(s) %s; choose from %s", unknown, ", ".join(catalog))
        return 2
    failed = 0
    for name in names or list(catalog):
        item = catalog[name]
        if name != "whisper" and (root / item.target).exists():
            logger.info("ℹ️ %s already there", item.target)
            continue
        try:
            if name == "whisper":
                _fetch_whisper(root)
            elif name.startswith("sherpa-"):
                _fetch_sherpa(root, item)
            else:
                _fetch_faster_whisper(root, item)
        except (httpx.HTTPError, OSError, tarfile.TarError, ValueError) as exc:
            logger.error("❌ %s: %s", name, exc)
            failed += 1
    return 1 if failed else 0
