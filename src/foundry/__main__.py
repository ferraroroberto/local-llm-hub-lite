"""``python -m src.foundry``: opt this PC into the Foundry Local (Nemotron) roster.

    python -m src.foundry status   # exit 0 when a foundry-local row is enabled and ready, else 1
    python -m src.foundry setup    # install the SDK extra, fetch and verify the exact variants,
                                   # enable the roster, then download what it needs

``setup`` is the installer's opt-in step (``setup.bat`` never runs it): it
pip-installs ``requirements-foundry.txt``, downloads the exact
``runtime_model`` of each ``foundry-local`` row the roster or this PC enables
and checks it is cached, copies
``config/models.foundry.yaml`` to the machine-local overlay
``config/models.local.yaml`` (never over an existing one), then runs
``src.install --fix`` so the roster's GGUF weights are downloaded too.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from typing import List, Optional

import yaml

from .. import host_profile
from ..model_registry import ENGINE_FOUNDRY, all_models, enabled_models
from ..no_window import NO_WINDOW
from ..runtime_files import checked_write_path
from . import runtime

logger = logging.getLogger(__name__)

REQUIREMENTS_FILE = host_profile.PROJECT_ROOT / "requirements-foundry.txt"
ROSTER_TEMPLATE_NAME = "models.foundry.yaml"
# A cold install pulls the SDK, its native core and onnxruntime wheels; 15 min
# is several slow runs and still frees the shell if pip wedges.
PIP_INSTALL_TIMEOUT_S = 900


def _foundry_rows(models) -> list:
    return [m for m in models if m.engine == ENGINE_FOUNDRY]


def _rows_to_fetch() -> list:
    """Foundry rows the roster template or this PC enables.

    A row neither one names is not opted into, so its weights are not
    downloaded here.
    """
    template = host_profile.local_overlay_path().with_name(ROSTER_TEMPLATE_NAME)
    roster = yaml.safe_load(template.read_text(encoding="utf-8"))
    wanted = set(roster["hosts"]["local"]["enabled"]) | set(host_profile.resolve().enabled)
    return [m for m in _foundry_rows(all_models()) if m.id in wanted]


def status() -> int:
    """Log the Foundry Local state; 0 when an enabled foundry-local row is ready, else 1."""
    sdk_state, detail = runtime.sdk_status()
    logger.info("ℹ️ SDK: %s", detail)
    rows = _foundry_rows(enabled_models())
    if not rows:
        logger.info("ℹ️ no foundry-local row is enabled on this PC (optional: python -m src.foundry setup)")
        return 1
    if sdk_state == "missing":
        return 1
    ready = True
    for m in rows:
        try:
            cached = runtime.is_cached(m.runtime_model or "")
        except runtime.FoundryError as exc:
            logger.error("❌ %s: %s", m.id, exc)
            ready = False
            continue
        logger.info("%s %s: %s %s", "✅" if cached else "⚠️", m.id, m.runtime_model,
                    "cached" if cached else "not cached")
        ready = ready and cached
    return 0 if ready else 1


def _install_sdk() -> None:
    if runtime.sdk_version() == runtime.SDK_PIN:
        logger.info("ℹ️ %s %s already installed", runtime.SDK_PACKAGE, runtime.SDK_PIN)
        return
    logger.info("ℹ️ installing %s", REQUIREMENTS_FILE.name)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
            check=True, creationflags=NO_WINDOW, timeout=PIP_INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise runtime.FoundryError(f"pip install timed out after {PIP_INSTALL_TIMEOUT_S}s") from None
    if runtime.sdk_version() != runtime.SDK_PIN:
        raise runtime.FoundryError(
            f"{runtime.SDK_PACKAGE} {runtime.SDK_PIN} not installed after pip (found {runtime.sdk_version()})"
        )


def _enable_roster() -> bool:
    """Copy the roster template to the overlay unless one exists; True when written."""
    overlay = host_profile.local_overlay_path()
    if overlay.exists():
        logger.info("ℹ️ %s already exists; left unchanged (merge %s by hand if wanted)",
                    overlay.name, ROSTER_TEMPLATE_NAME)
        return False
    shutil.copyfile(overlay.with_name(ROSTER_TEMPLATE_NAME), checked_write_path(overlay))
    host_profile._CONFIG_CACHE.clear()
    logger.info("✅ wrote %s from %s", overlay.name, ROSTER_TEMPLATE_NAME)
    return True


def prepare() -> None:
    """Install the pinned SDK and fetch the variants :func:`_rows_to_fetch` names.

    Raises :class:`runtime.FoundryError`. This is also ``src.install --fix``'s
    repair for a missing SDK or variant, so it must not call back into it.
    """
    _install_sdk()
    for m in _rows_to_fetch():
        runtime.download(m.runtime_model or "")


def setup(*, enable: bool = True) -> int:
    try:
        prepare()
    except runtime.FoundryError as exc:
        logger.error("❌ %s", exc)
        return 1
    if enable:
        _enable_roster()
    from .. import install

    return install.main(["--fix"])


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.foundry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="exit 0 when an enabled foundry-local row is ready")
    p_setup = sub.add_parser("setup", help="install, fetch and enable the Foundry Local roster")
    p_setup.add_argument("--no-enable", action="store_true",
                         help="install and fetch only; leave config/models.local.yaml alone")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        return status()
    return setup(enable=not args.no_enable)


if __name__ == "__main__":
    sys.exit(main())
