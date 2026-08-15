"""Shared helpers for the vendor-binary install scripts.

``install_llama_cpp.py`` and ``install_whisper_cpp.py`` both download a
GitHub release archive, extract it into a ``vendor/`` directory, and
collapse a single-subdir extraction — near line-for-line duplicates before
this module existed (issue #195). Consolidated here so a fix to the
download/extract/flatten logic lands once instead of twice.

Not a package ``__init__`` — a plain sibling module. Each install script
inserts its own directory onto ``sys.path`` before importing this (works
whether the script runs directly as ``python scripts/install_*.py`` or is
imported as ``scripts.install_*`` from ``src/install.py``'s admin "Fix"
dispatch).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class InstallError(RuntimeError):
    pass


def no_window_flags() -> int:
    """CREATE_NO_WINDOW on Windows — this also runs from the windowless hub
    when triggered via the admin SPA's "Fix" button (issue #174)."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def detect_cuda_arch() -> str:
    """The host GPU's CUDA compute capability as a bare arch (e.g. ``61`` for a
    GTX 1070's sm_61), from ``nvidia-smi --query-gpu=compute_cap``. Empty string
    if nvidia-smi is absent/unreadable — the caller supplies a default. Used by
    the Linux from-source build hints (#368), where the vendored-release path
    upstream ships for Windows/macOS has no Linux equivalent.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, creationflags=no_window_flags(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip().replace(".", "")
    except Exception:  # noqa: BLE001 — best-effort probe
        pass
    return ""


def asset_sha256(asset: dict) -> Optional[str]:
    """Extract the expected sha256 from a GitHub release asset's ``digest``
    field (``"sha256:<hex>"``), if the API returned one. Older assets (or a
    GitHub Enterprise instance that hasn't backfilled digests) omit it —
    callers treat ``None`` as "nothing to verify against" rather than an
    error, since this is a hardening check, not a hard requirement."""
    digest = (asset or {}).get("digest") or ""
    prefix = "sha256:"
    if digest.lower().startswith(prefix):
        return digest[len(prefix):].strip().lower()
    return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, expected_sha256: Optional[str] = None) -> str:
    """Download ``url`` to ``dest``, returning the sha256 of the bytes written.

    When ``expected_sha256`` is given (from :func:`asset_sha256`), the
    downloaded file is verified against it and removed + rejected on a
    mismatch — a corrupted or tampered download must never reach ``extract()``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    log.info("       -> %s", dest)
    with urllib.request.urlopen(url, timeout=120) as r:
        total = int(r.headers.get("Content-Length", 0))
        seen = 0
        next_report = 0
        with dest.open("wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                seen += len(chunk)
                if total and seen >= next_report:
                    pct = 100 * seen / total
                    log.info("  %6.1f / %6.1f MB (%5.1f%%)", seen/1_048_576, total/1_048_576, pct)
                    next_report = seen + total // 20
    log.info("  done: %.1f MB", seen/1_048_576)

    digest = _sha256_of(dest)
    log.info("  sha256: %s", digest)
    if expected_sha256 is not None and not hmac.compare_digest(digest, expected_sha256):
        dest.unlink(missing_ok=True)
        raise InstallError(
            f"sha256 mismatch for {dest.name}: expected {expected_sha256}, got {digest}"
        )
    return digest


def _reject_unsafe_members(names: List[str], dest_dir: Path) -> None:
    """Raise if any archive member would extract outside ``dest_dir``.

    Belt-and-suspenders on top of the stdlib's own member-path handling
    (``zipfile`` sanitises ``..``/drive components; ``tarfile``'s
    ``filter="data"`` does likewise for tar) — resolves each member's
    target path and confirms it stays under ``dest_dir``.
    """
    dest_resolved = dest_dir.resolve()
    for name in names:
        target = (dest_dir / name).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise InstallError(f"archive member escapes destination: {name!r}")


def extract(archive: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info("extracting %s -> %s", archive.name, dest_dir)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            _reject_unsafe_members(zf.namelist(), dest_dir)
            zf.extractall(dest_dir)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            _reject_unsafe_members(tf.getnames(), dest_dir)
            # filter="data": rejects absolute paths, `..` traversal, device
            # files, and symlinks/hardlinks that escape dest_dir. Required
            # explicitly on 3.12/3.13 (unfiltered extraction warns); becomes
            # the stdlib default in 3.14.
            tf.extractall(dest_dir, filter="data")
    else:
        raise InstallError(f"unknown archive type: {archive}")


def flatten_if_nested(target: Path) -> None:
    """Some releases extract into a single subdir; collapse it into target."""
    entries = [p for p in target.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(target / child.name))
        inner.rmdir()


def flatten_nested_bin(vendor_dir: Path, binary_name: str) -> None:
    """Lift a nested ``bin/``-style directory holding ``binary_name`` up into
    ``vendor_dir``, so sibling files (DLLs, configs) travel with it.

    Some release archives put the server binary a level or two down (e.g.
    ``build/bin/``) instead of at the archive root. Finds the binary via
    ``rglob``, then moves every sibling in its parent directory up into
    ``vendor_dir``, overwriting an existing file/dir of the same name.
    No-op if the binary is already directly in ``vendor_dir``, or absent.
    Both install scripts wrote this walk-and-lift by hand before this
    helper existed (issue #5) — consolidated here for the same reason
    ``flatten_if_nested`` was.
    """
    for candidate in vendor_dir.rglob(binary_name):
        src_dir = candidate.parent
        if src_dir == vendor_dir:
            return
        log.info("flattening %s -> %s", src_dir, vendor_dir)
        for child in list(src_dir.iterdir()):
            target = vendor_dir / child.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), str(target))
        return
