"""Per-model serving state, declared once and derived in one place.

A backend's process being alive says nothing about whether it answers: a
llama-server reading several GB of weights, one that crashed during load and
one that hung are all "alive or not" without being ready. This module names
the states a reader needs and derives them from one set of observations, so
the Models tab, the audio health route and the on-demand loader cannot
disagree about a model. ``docs/serving-state.md`` has the state table.

States:

* ``stopped`` — no process of ours, nothing on the port.
* ``loading`` — ours, spawned (or inherited) and not yet answering, still
  inside the load deadline.
* ``ready`` — ours and answering.
* ``adopted`` — someone else's process holds the port (the hub routes to it
  but did not start it).
* ``failed`` — ours died on its own, never answered within the deadline, or
  stopped answering after it was ready; ``reason`` says which.
* ``unknown`` — a fact needed to tell the states apart could not be gathered
  (the listening-port snapshot or the readiness probe), or the facts
  contradict each other.

:func:`derive` is pure: it reads one frozen :class:`Observation` and returns
a :class:`Serving`. ``backend_process.serving`` gathers the observation and
latches the launch record's ``ready`` flag, so the only mutation lives with
the code that owns the process handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .server_process import OWNERSHIP_EXTERNAL, OWNERSHIP_NONE, OWNERSHIP_OURS

# How long a spawned backend may take to answer before it reads as failed.
# Sized for the heaviest row (a multi-GB GGUF read from disk); it is also the
# budget the on-demand first request waits (``on_demand.READY_DEADLINE_S``).
LOAD_DEADLINE_S = 180.0

class ServingState(str, Enum):
    """What a model is doing right now. Values travel as ``state``."""

    STOPPED = "stopped"
    LOADING = "loading"
    READY = "ready"
    ADOPTED = "adopted"
    FAILED = "failed"
    UNKNOWN = "unknown"


# States in which the model serves requests: what "running" means to a reader.
SERVING = frozenset({ServingState.READY, ServingState.ADOPTED})


@dataclass
class LaunchRecord:
    """One spawn (or inheritance) of a backend the hub owns.

    ``spawned_at`` is ``time.monotonic()`` at spawn. ``ready`` latches the
    first time the live process answers, so a later loss of the probe reads
    as "stopped answering" rather than "still loading". A deliberate stop
    drops the record, which is what tells "stopped" from "died on its own".
    """

    spawned_at: float
    ready: bool = False


@dataclass(frozen=True)
class Observation:
    """What a reader saw for one model at one moment.

    ``probe`` is ``None`` when the readiness probe could not be run; and
    ``port_held`` is ``None`` when the listening-port snapshot failed. Both
    are unknown, never a negative.
    """

    alive: bool                         # our Popen, or an adopted PID whose identity still matches
    exit_code: Optional[int]            # our Popen's exit code once it died
    launched: bool                      # a launch record exists (the hub never stopped it)
    was_ready: bool                     # that launch answered a probe at least once
    load_age_s: Optional[float]         # seconds since that launch
    probe: Optional[bool]               # the backend answered its probe; None = not run
    port_held: Optional[bool]           # someone listens on the port; None = snapshot failed
    pid: Optional[int] = None           # ours while alive, else the port holder's


@dataclass(frozen=True)
class Serving:
    """A derived state plus the reason a ``failed`` / ``unknown`` state carries.

    ``ownership`` and ``pid`` are the older tri-state fields, derived here so
    no reader computes them on its own. An ``unknown`` state keeps ownership
    ``none``, the value older clients already read; ``state`` is what tells it
    apart from a confirmed stop.
    """

    state: ServingState
    reason: Optional[str] = None
    ownership: str = OWNERSHIP_NONE
    pid: Optional[int] = None


def _exited(exit_code: Optional[int], was_ready: bool) -> str:
    when = "after it was ready" if was_ready else "during load"
    code = f" (exit code {exit_code})" if exit_code is not None else ""
    return f"process exited {when}{code}"


def _derive_alive(obs: Observation, deadline_s: float) -> Serving:
    ours = {"ownership": OWNERSHIP_OURS, "pid": obs.pid}
    if obs.probe is None:
        return Serving(ServingState.UNKNOWN, "readiness probe could not be run", **ours)
    if obs.probe:
        return Serving(ServingState.READY, **ours)
    if not obs.launched:
        return Serving(ServingState.UNKNOWN, "process is alive but has no launch record", **ours)
    if obs.was_ready:
        return Serving(ServingState.FAILED, "process is alive but stopped answering", **ours)
    if obs.load_age_s is not None and obs.load_age_s > deadline_s:
        return Serving(
            ServingState.FAILED,
            f"did not answer within {deadline_s:.0f}s of starting", **ours,
        )
    return Serving(ServingState.LOADING, **ours)


def derive(obs: Observation, deadline_s: float = LOAD_DEADLINE_S) -> Serving:
    """The one derivation of a model's serving state from what a reader saw."""
    if obs.alive:
        return _derive_alive(obs, deadline_s)
    if obs.port_held:
        return Serving(ServingState.ADOPTED, ownership=OWNERSHIP_EXTERNAL, pid=obs.pid)
    # Not alive, but the hub never stopped it: it died on its own.
    if obs.launched:
        return Serving(ServingState.FAILED, _exited(obs.exit_code, obs.was_ready))
    if obs.port_held is None:
        if obs.probe:
            return Serving(ServingState.ADOPTED, ownership=OWNERSHIP_EXTERNAL)
        return Serving(ServingState.UNKNOWN, "listening-port snapshot failed")
    if obs.probe:
        return Serving(ServingState.UNKNOWN, "answers its probe but no process holds the port")
    return Serving(ServingState.STOPPED)
