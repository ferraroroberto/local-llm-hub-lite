"""Foundry Local runtime helper: SDK presence, exact catalog resolve, start or reuse.

The SDK (``foundry-local-sdk``, pinned in ``requirements-foundry.txt``) is an
optional extra: nothing here imports it at module load, so a PC without it
still imports the hub and serves its llama.cpp / whisper.cpp rows. Every
entry point that needs the SDK raises :class:`FoundryError` carrying
:data:`NOT_INSTALLED` instead.

The SDK runs the model in-process (its native core ships in the wheel), so
no Foundry Local CLI or service is required.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

SDK_PACKAGE = "foundry-local-sdk"
# The version this hub's worker is written against: the 1.x API exposes
# ``Catalog.get_model_variant`` and the live transcription session the
# streaming Nemotron ASR variant needs. 2.x is a different in-process API.
SDK_PIN = "1.2.4"
# Names the SDK's per-app data folder (``~/.local-llm-hub-lite``), which
# holds its model cache; the hub and its setup command share it.
APP_NAME = "local-llm-hub-lite"
NOT_INSTALLED = (
    "Foundry Local not installed: run `.venv\\Scripts\\python.exe -m src.foundry setup` "
    "(installs requirements-foundry.txt)"
)

# ``ensure_loaded`` outcomes.
LOADED_STARTED = "started"
LOADED_REUSED = "reused"


class FoundryError(RuntimeError):
    """Foundry Local cannot serve the requested model; the message says why."""


def sdk_version() -> Optional[str]:
    """Installed ``foundry-local-sdk`` version, or ``None`` when it is not installed."""
    try:
        return importlib.metadata.version(SDK_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def sdk_status() -> Tuple[str, str]:
    """``(status, detail)``: ``missing`` without the SDK, ``warn`` off the pin, else ``ok``."""
    version = sdk_version()
    if version is None:
        return "missing", NOT_INSTALLED
    if version != SDK_PIN:
        return "warn", f"{SDK_PACKAGE} {version} installed; this hub is written against {SDK_PIN}"
    return "ok", f"{SDK_PACKAGE} {version}"


def _sdk() -> Any:
    try:
        import foundry_local_sdk
    except ImportError as exc:
        raise FoundryError(NOT_INSTALLED) from exc
    return foundry_local_sdk


def open_catalog() -> Any:
    """The SDK catalog, initializing the process-wide manager on first use."""
    sdk = _sdk()
    manager = sdk.FoundryLocalManager.instance
    if manager is None:
        sdk.FoundryLocalManager.initialize(sdk.Configuration(app_name=APP_NAME))
        manager = sdk.FoundryLocalManager.instance
    return manager.catalog


def resolve_variant(catalog: Any, runtime_model: str) -> Any:
    """The catalog variant whose id is exactly ``runtime_model``.

    An alias (no ``:version``) or a different variant is refused: the row
    names one variant and the worker must load that one, never the
    catalog's preferred pick for an alias.
    """
    if not runtime_model or ":" not in runtime_model:
        raise FoundryError(
            f"runtime model {runtime_model!r} is not an exact catalog variant id (name:version)"
        )
    variant = catalog.get_model_variant(runtime_model)
    if variant is None:
        raise FoundryError(f"runtime model {runtime_model!r} is not in the Foundry Local catalog")
    if variant.id != runtime_model:
        raise FoundryError(
            f"catalog resolved {runtime_model!r} to {variant.id!r}; refusing a different variant"
        )
    return variant


def download(runtime_model: str, progress: Optional[Callable[[float], None]] = None) -> Any:
    """Resolve ``runtime_model`` and fetch it into the cache unless it is already there."""
    variant = resolve_variant(open_catalog(), runtime_model)
    if variant.is_cached:
        logger.info("ℹ️ %s already cached", variant.id)
        return variant
    logger.info("ℹ️ downloading %s", variant.id)
    variant.download(progress)
    if not variant.is_cached:
        raise FoundryError(f"download of {variant.id} finished but the variant is not cached")
    logger.info("✅ %s cached", variant.id)
    return variant


def is_cached(runtime_model: str) -> bool:
    """Whether the exact variant is in the local cache (raises :class:`FoundryError`)."""
    return bool(resolve_variant(open_catalog(), runtime_model).is_cached)


def ensure_loaded(runtime_model: str, *, allow_download: bool = True) -> Tuple[Any, str]:
    """Start or reuse ``runtime_model``: ``(variant, LOADED_STARTED | LOADED_REUSED)``.

    Reuses a variant this process already has loaded; otherwise fetches it
    when missing (``allow_download``) and loads it.
    """
    variant = resolve_variant(open_catalog(), runtime_model)
    if variant.is_loaded:
        logger.info("ℹ️ reusing loaded %s", variant.id)
        return variant, LOADED_REUSED
    if not variant.is_cached:
        if not allow_download:
            raise FoundryError(f"{variant.id} is not cached; run `python -m src.foundry setup`")
        download(runtime_model)
    variant.load()
    logger.info("✅ loaded %s", variant.id)
    return variant, LOADED_STARTED
