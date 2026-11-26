"""Resolve the host profile from config/models.yaml.

The ``hosts:`` block holds exactly one row (this fork serves a single
machine): its ``enabled:`` list names the registry rows the hub serves and
its optional ``vram_mb`` is the on-demand overcommit ceiling.

``_load_config()`` below is also the single cached YAML loader for
``config/models.yaml`` — ``src/model_registry.py`` imports it directly
rather than keeping its own parallel cache of the same file, and the
admin roles router reads the raw ``roles:`` block through it. A
machine-local ``config/models.local.yaml`` beside it is deep-merged on top.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "models.yaml"
# Machine-local overlay beside ``CONFIG_PATH`` (gitignored): this PC's own
# choices (enabled list, role targets, a row's startup) without editing the
# tracked registry, so a pull never conflicts with them.
LOCAL_OVERLAY_NAME = "models.local.yaml"


def local_overlay_path() -> Path:
    """The overlay that sits beside the active ``CONFIG_PATH``."""
    return CONFIG_PATH.with_name(LOCAL_OVERLAY_NAME)


def merge_overlay(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge ``overlay`` into a copy of ``base``: mappings merge, anything else replaces."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_overlay(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class HostProfile:
    id: str
    enabled: List[str]
    # Discrete-GPU VRAM ceiling in MB. The on-demand lifecycle sums
    # the running models' ``est_vram_mb`` and warns (advisory only, never
    # blocks) when a load would exceed this. Unset disables the warning.
    vram_mb: Optional[int] = None


# Parsed-YAML cache, keyed by the resolved config path. The README's
# contract is "edit the YAML and restart the hub to pick up changes," so a
# single hub request shouldn't pay several YAML parses behind the scenes
# (resolve() + hub_port() + hub_bind_host() + model_registry's all_models()/
# desired_model_ids() all read the same file). Keying on the path means
# swapping ``CONFIG_PATH`` (as the tests do) transparently busts the cache.
_CONFIG_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_config() -> Dict[str, Any]:
    key = str(CONFIG_PATH)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config file missing: {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    overlay_path = local_overlay_path()
    if overlay_path.is_file():
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        if not isinstance(overlay, dict):
            raise ValueError(f"{overlay_path} must be a YAML mapping")
        data = merge_overlay(data, overlay)
    _CONFIG_CACHE[key] = data
    return data


def resolve() -> HostProfile:
    """Return the profile for the sole ``hosts:`` row."""
    hosts: Dict[str, Any] = _load_config().get("hosts") or {}
    if not hosts:
        raise RuntimeError(f"no 'hosts' defined in {CONFIG_PATH}")
    host_id, row = next(iter(hosts.items()))
    return HostProfile(
        id=host_id,
        enabled=list(row.get("enabled", []) or []),
        vram_mb=int(row["vram_mb"]) if row.get("vram_mb") is not None else None,
    )


# Beside the full hub's :8000 on one machine, so the lite hub defaults to its own port.
DEFAULT_HUB_PORT = 8020


def hub_port() -> int:
    cfg = _load_config()
    return int((cfg.get("hub") or {}).get("port", DEFAULT_HUB_PORT))


def hub_bind_host() -> str:
    cfg = _load_config()
    return str((cfg.get("hub") or {}).get("bind_host", "0.0.0.0"))


if __name__ == "__main__":
    # The .bat launchers ask for the port here, so no script repeats the number.
    sys.stdout.write(f"{hub_port()}\n")
