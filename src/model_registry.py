"""Load models.yaml and expose typed entries filtered by the active host.

One source of truth for: which models exist, how to launch them, which
port they listen on, and how the hub should route by model-name alias.

Lite fork: single-host only. A row may still carry a bare ``host:`` (or
none — "local"), but there are no ordered host chains, no CPU-offload
tiers, and no fleet placement. The ``hosts:`` block in the YAML holds
exactly one row (with ``default: true`` and an ``enabled:`` list).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .host_profile import HostProfile, _load_config, resolve as resolve_host

# ``startup:`` policy values (#422) — see the field docstring on ``Model``.
STARTUP_EAGER = "eager"
STARTUP_ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class Model:
    id: str                         # short key in YAML ("qwen", "whisper")
    display_name: str               # name the client sends in the `model` field
    backend: str                    # "openai" | "whisper"
    aliases: List[str] = field(default_factory=list)
    engine: Optional[str] = None
    port: Optional[int] = None
    hf_repo: Optional[str] = None
    hf_pattern: Optional[str] = None
    model_path: Optional[str] = None
    args: List[str] = field(default_factory=list)
    # A *virtual* model is an alias of another backend: it shares an existing
    # backend's ``port`` (so ``url`` already points at the running process) and
    # has no engine / weights of its own. It is never launched, downloaded, or
    # offered as a controllable process in the admin UI — it only exists to
    # route the chat shape with an ``inject_extra`` overlay.
    virtual: bool = False
    # Server-side defaults folded into the upstream OpenAI ``extra`` payload on
    # every /v1/chat/completions for this id (caller-sent fields win). Used by
    # the no-think alias to deliver ``chat_template_kwargs={enable_thinking:
    # false}`` to clients that can't send it themselves.
    inject_extra: Optional[Dict[str, Any]] = None
    # Owning host id — a bare ``host: <id>`` in the YAML. Unset means "local
    # everywhere" (which, on a single-host config, is the same thing).
    host: Optional[str] = None
    # Rough static GPU-VRAM footprint in MB (#375) — enough for the on-demand
    # overcommit warning, NOT live telemetry. CPU-only / virtual rows are 0;
    # a None value contributes 0 to any sum.
    est_vram_mb: Optional[int] = None
    # Process-lifecycle policy (#422). ``eager`` (the default) means the
    # model is autostarted with the hub. ``on_demand`` means nothing starts
    # it eagerly: the first request that routes to it spawns the backend
    # (the request waits for readiness), and ``src.on_demand``'s idle
    # watchdog stops it again after ``idle_unload_minutes`` without traffic.
    # Any unknown value is normalized to ``eager`` so a typo can't silently
    # disable a model.
    startup: str = STARTUP_EAGER
    # Only meaningful for ``startup: on_demand`` — minutes without a request
    # after which the idle watchdog unloads the backend. ``None`` disables
    # idle unload (the model stays up once demanded until stopped by hand).
    idle_unload_minutes: Optional[int] = None

    @property
    def all_names(self) -> List[str]:
        # The registry ``id`` is the canonical handle used by tools that
        # walk the YAML (the SPA Playground dropdown, etc.). Include it so
        # /v1/models lists every name a client can send.
        names = [self.id, self.display_name, *self.aliases]
        return [n for n in dict.fromkeys(names) if n]

    @property
    def url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self.port}/v1" if self.port else None


# ``_load_config()`` (imported above) is host_profile's cached YAML loader
# for config/models.yaml — both modules read the same file, so there is one
# cache rather than two kept in sync by convention.


def _parse_startup(row: Dict) -> str:
    """Normalize a row's ``startup:`` to a known policy (#422).

    Anything other than the literal ``on_demand`` reads as ``eager`` — a
    typo'd value must degrade to the always-on behavior, never to a
    model that silently refuses to start.
    """
    raw = str(row.get("startup") or STARTUP_EAGER).strip().lower()
    return STARTUP_ON_DEMAND if raw == STARTUP_ON_DEMAND else STARTUP_EAGER


def _row_to_model(model_id: str, row: Dict) -> Model:
    return Model(
        id=model_id,
        display_name=str(row.get("display_name") or model_id),
        backend=str(row.get("backend", "openai")),
        aliases=list(row.get("aliases", []) or []),
        engine=row.get("engine"),
        port=int(row["port"]) if row.get("port") is not None else None,
        hf_repo=row.get("hf_repo"),
        hf_pattern=row.get("hf_pattern"),
        model_path=row.get("model_path"),
        args=list(row.get("args", []) or []),
        virtual=bool(row.get("virtual", False)),
        inject_extra=row.get("inject_extra") or None,
        host=str(row["host"]) if row.get("host") else None,
        est_vram_mb=int(row["est_vram_mb"]) if row.get("est_vram_mb") is not None else None,
        startup=_parse_startup(row),
        idle_unload_minutes=(
            int(row["idle_unload_minutes"])
            if row.get("idle_unload_minutes") is not None else None
        ),
    )


def all_models() -> List[Model]:
    """Every configured row, exactly as declared in YAML."""
    cfg = _load_config()
    rows: Dict = cfg.get("models") or {}
    return [_row_to_model(mid, row) for mid, row in rows.items()]


def enabled_models(host: Optional[HostProfile] = None) -> List[Model]:
    """Return models the active host is configured to serve — rows named in
    the host's ``enabled:`` list."""
    profile = host or resolve_host()
    whitelist = set(profile.enabled)
    return [m for m in all_models() if m.id in whitelist]


def local_models(host: Optional[HostProfile] = None) -> List[Model]:
    """``enabled_models()`` filtered to rows this host may actually run —
    excludes any row whose ``host:`` names a different machine. The
    generalization of "give me the models I might spawn, manage, download
    weights for, or health-check": install checks, port-liveness checks,
    spawn/inherit loops, tray menus."""
    profile = host or resolve_host()
    return [m for m in enabled_models(host) if not m.host or m.host == profile.id]


# Backends that own a spawnable local process. A `virtual` row shares
# another model's process. Everything a `run_backend <id>` can actually
# start is one of these engines and non-virtual.
_SPAWNABLE_BACKENDS = ("openai", "whisper")


def launchable_local_ids(host: Optional[HostProfile] = None) -> List[str]:
    """Ids of models this host can actually spawn as its own local process.

    ``local_models()`` narrowed to rows with a spawnable backend, dropping
    virtual aliases (which share another row's process). This is exactly the
    set ``run_backend <id>`` starts without erroring — the single source the
    bulk launchers enumerate so they can never drift from the active host's
    ``enabled:`` contract.
    """
    return [
        m.id for m in local_models(host)
        if m.backend in _SPAWNABLE_BACKENDS and not m.virtual
    ]


def desired_model_ids(host: Optional[HostProfile] = None) -> List[str]:
    """The models ``host`` should be running right now, derived from the
    registry (#430) — no separate desired-state file.

    A model belongs to the desired running set when it is launchable here
    (enabled ∧ spawnable ∧ non-virtual, exactly ``launchable_local_ids``)
    and declares ``startup: eager`` (#422 — ``on_demand`` rows are never
    desired; ``src.on_demand`` owns their lifecycle). This one derivation
    feeds hub autostart on boot
    (``server_lifecycle._autostart_configured_backends``).
    """
    profile = host or resolve_host()
    launchable = set(launchable_local_ids(profile))
    return [
        m.id for m in all_models()
        if m.id in launchable and m.startup == STARTUP_EAGER
    ]


def resolve(name: str, host: Optional[HostProfile] = None) -> Optional[Model]:
    """Look up a model by any of its names — registry id, display_name, or alias.

    Accepting ``id`` matters for tools that drive the hub off the YAML
    directly: the SPA Playground dropdown sends ``m.id`` (the YAML key),
    and the hub's own ``run_backend`` picks the entry by id. Without this,
    every model whose id is not also listed in ``aliases`` 400'd on the
    Playground.
    """
    name = name.strip()
    for m in enabled_models(host):
        if name == m.id or name == m.display_name or name in m.aliases:
            return m
    return None


def resolve_any(name: str) -> Optional[Model]:
    """Look up a model by any of its names across **every** configured row,
    ignoring the active host's ``enabled:`` whitelist.

    The host-blind companion to :func:`resolve`. Answers a different question:
    not "can I serve this here?" but "is this name one of *our* model ids at
    all?". The audio proxy needs exactly that distinction to keep an explicit
    ``model=`` strict (issue #412): a configured id that can't be served here
    must fail loudly rather than be answered by a different model, while a
    name the registry has never heard of (a client-side placeholder like
    OpenAI's ``whisper-1``) is not a model request at all and still addresses
    the transcription role.

    Matching is **case-insensitive** (and whitespace-tolerant), unlike
    :func:`resolve`. That asymmetry is deliberate: this function answers "did
    the caller *name* one of our models?", and someone who typed ``Whisper``
    named whisper. :func:`resolve` stays exact because its callers (dispatch,
    ``/v1/models``, the admin model control) key off the canonical name; pair
    the two by resolving *this* row's ``id`` there.
    """
    key = (name or "").strip().lower()
    if not key:
        return None
    for m in all_models():
        if key in {n.lower() for n in m.all_names}:
            return m
    return None
