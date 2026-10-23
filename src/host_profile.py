"""Resolve the host profile from config/models.yaml.

The ``hosts:`` block holds exactly one row (this fork serves a single
machine): its ``enabled:`` list names the registry rows the hub serves and
its optional ``vram_mb`` is the on-demand overcommit ceiling.

``_load_config()`` below is also the single cached YAML loader for
``config/models.yaml`` — ``src/model_registry.py`` imports it directly
rather than keeping its own parallel cache of the same file, and the
admin roles router reads the raw ``roles:`` block through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "models.yaml"


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


def hub_port() -> int:
    cfg = _load_config()
    return int((cfg.get("hub") or {}).get("port", 8000))


def hub_bind_host() -> str:
    cfg = _load_config()
    return str((cfg.get("hub") or {}).get("bind_host", "0.0.0.0"))
