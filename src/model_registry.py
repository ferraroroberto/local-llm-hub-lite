"""Load models.yaml and expose typed entries filtered by the host's ``enabled:`` list.

One source of truth for: which models exist, how to launch them, which
port they listen on, and how the hub should route by model-name alias.

Single-host: the ``hosts:`` block in the YAML holds exactly one row with an
``enabled:`` list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .host_profile import HostProfile, _load_config
from .host_profile import resolve as resolve_host

logger = logging.getLogger(__name__)

# ``startup:`` policy values — see the field docstring on ``Model``.
STARTUP_EAGER = "eager"
STARTUP_ON_DEMAND = "on_demand"

# ``engine:`` values. Foundry Local rows run an in-process SDK worker
# (``src.foundry.worker``) instead of a vendored binary.
ENGINE_LLAMA = "llama-server"
ENGINE_WHISPER = "whisper-server"
ENGINE_FOUNDRY = "foundry-local"


class RegistryError(ValueError):
    """A models.yaml row that cannot be served as declared."""


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
    # Rough static GPU-VRAM footprint in MB — enough for the on-demand
    # overcommit warning, NOT live telemetry. CPU-only / virtual rows are 0;
    # a None value contributes 0 to any sum.
    est_vram_mb: Optional[int] = None
    # Process-lifecycle policy. ``eager`` (the default) means the
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
    # Exact runtime catalog variant a ``foundry-local`` row serves
    # (``name:version``, e.g. ``nemotron-3.5-asr-streaming-0.6b-generic-cpu:3``).
    # The worker loads exactly this id and reports it on ``/health``.
    runtime_model: Optional[str] = None

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
    """Normalize a row's ``startup:`` to a known policy.

    Anything other than the literal ``on_demand`` reads as ``eager`` — a
    typo'd value must degrade to the always-on behavior, never to a
    model that silently refuses to start.
    """
    raw = str(row.get("startup") or STARTUP_EAGER).strip().lower()
    return STARTUP_ON_DEMAND if raw == STARTUP_ON_DEMAND else STARTUP_EAGER


def _validate_row(model_id: str, row: Dict) -> None:
    """Refuse a row whose declared kind would serve something other than it names.

    A virtual alias routes to another backend's port, so a row that is both
    ``virtual`` and runtime-backed would answer ``/v1/models`` with this id
    while a different model runs. Aliases and runtime-backed models are
    separate classes; a Foundry row must also name an exact catalog variant.
    """
    engine = row.get("engine")
    runtime_model = row.get("runtime_model")
    if row.get("virtual") and (runtime_model or engine == ENGINE_FOUNDRY):
        raise RegistryError(
            f"model {model_id!r} is declared virtual (an alias of another backend) "
            f"but names runtime model {runtime_model or engine!r}; an alias cannot "
            f"stand in for a runtime-backed model"
        )
    if engine == ENGINE_FOUNDRY:
        if row.get("backend") != "whisper":
            raise RegistryError(
                f"model {model_id!r}: engine {ENGINE_FOUNDRY} serves ASR rows only (backend: whisper)"
            )
        if not runtime_model or ":" not in str(runtime_model):
            raise RegistryError(
                f"model {model_id!r}: engine {ENGINE_FOUNDRY} needs runtime_model set to an exact "
                f"catalog variant id (name:version), got {runtime_model!r}"
            )


def _row_to_model(model_id: str, row: Dict) -> Model:
    _validate_row(model_id, row)
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
        est_vram_mb=int(row["est_vram_mb"]) if row.get("est_vram_mb") is not None else None,
        startup=_parse_startup(row),
        idle_unload_minutes=(
            int(row["idle_unload_minutes"])
            if row.get("idle_unload_minutes") is not None else None
        ),
        runtime_model=str(row["runtime_model"]) if row.get("runtime_model") else None,
    )


def registry_problems() -> Dict[str, str]:
    """``{model_id: reason}`` for every row :func:`all_models` refuses to serve."""
    rows: Dict = _load_config().get("models") or {}
    problems: Dict[str, str] = {}
    for mid, row in rows.items():
        try:
            _validate_row(mid, row or {})
        except RegistryError as exc:
            problems[mid] = str(exc)
    return problems


def all_models() -> List[Model]:
    """Every configured row that can be served as declared.

    A refused row (see :func:`_validate_row`) is left out and logged, so it
    is never listed or routed to; :func:`registry_problems` names it.
    """
    cfg = _load_config()
    rows: Dict = cfg.get("models") or {}
    out: List[Model] = []
    for mid, row in rows.items():
        try:
            out.append(_row_to_model(mid, row or {}))
        except RegistryError as exc:
            logger.error("❌ registry row refused: %s", exc)
    return out


def transcribe_role_target() -> Optional[str]:
    """Registry id the ``roles.audio.transcribe`` card names, or ``None``."""
    roles = _load_config().get("roles") or {}
    audio = roles.get("audio") or {}
    target = (audio.get("transcribe") or {}).get("model_id")
    return str(target) if target else None


def enabled_models(host: Optional[HostProfile] = None) -> List[Model]:
    """Return models the active host is configured to serve — rows named in
    the host's ``enabled:`` list."""
    profile = host or resolve_host()
    whitelist = set(profile.enabled)
    return [m for m in all_models() if m.id in whitelist]


# Backends that own a spawnable local process. A `virtual` row shares
# another model's process. Everything a `run_backend <id>` can actually
# start is one of these engines and non-virtual.
_SPAWNABLE_BACKENDS = ("openai", "whisper")


def is_spawnable(model: Model) -> bool:
    """True when ``model``'s backend runs a local server process (virtual or not)."""
    return model.backend in _SPAWNABLE_BACKENDS


def is_launchable(model: Model) -> bool:
    """True when ``model`` owns its own spawnable process: spawnable, not a virtual alias."""
    return is_spawnable(model) and not model.virtual


def launchable_local_ids(host: Optional[HostProfile] = None) -> List[str]:
    """Ids of models this host can actually spawn as its own local process.

    ``enabled_models()`` narrowed to rows with a spawnable backend, dropping
    virtual aliases (which share another row's process). This is exactly the
    set ``run_backend <id>`` starts without erroring — the single source the
    bulk launchers enumerate so they can never drift from the active host's
    ``enabled:`` contract.
    """
    return [
        m.id for m in enabled_models(host) if is_launchable(m)
    ]


def desired_model_ids(host: Optional[HostProfile] = None) -> List[str]:
    """The models ``host`` should be running right now, derived from the
    registry — no separate desired-state file.

    A model belongs to the desired running set when it is launchable here
    (enabled ∧ spawnable ∧ non-virtual, exactly ``launchable_local_ids``)
    and declares ``startup: eager`` (``on_demand`` rows are never
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
    ``model=`` strict: a configured id that can't be served here
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
