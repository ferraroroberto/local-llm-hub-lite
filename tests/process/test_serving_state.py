"""The serving state machine: one pure ``derive`` over one ``Observation``.

The table covers every reachable observation, the impossible combinations
(ready with no process, loading with a dead process, a probe that was never
run) and every ``unknown`` case. The exhaustive test sweeps the whole
observation space and asserts the rules no state may break.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from src.process.serving_state import (
    LOAD_DEADLINE_S,
    Observation,
    ServingState,
    derive,
)

S = ServingState


def obs(**kw: Any) -> Observation:
    base: dict[str, Any] = dict(
        alive=False, exit_code=None, launched=False, was_ready=False,
        load_age_s=None, probe=False, port_held=False, pid=None,
        holder_confirmed=None,
    )
    base.update(kw)
    return Observation(**base)


# (case, observation, state, ownership, reason fragment or None)
ROWS = [
    ("nothing running, nothing listening", obs(), S.STOPPED, "none", None),
    ("stopped with the probe not run", obs(probe=None), S.STOPPED, "none", None),
    ("spawned, still reading weights",
     obs(alive=True, launched=True, load_age_s=5.0, pid=42), S.LOADING, "ours", None),
    ("loading right up to the deadline",
     obs(alive=True, launched=True, load_age_s=LOAD_DEADLINE_S, pid=42), S.LOADING, "ours", None),
    ("loading past the deadline",
     obs(alive=True, launched=True, load_age_s=LOAD_DEADLINE_S + 1, pid=42),
     S.FAILED, "ours", "did not answer within 180s"),
    ("spawned and answering",
     obs(alive=True, launched=True, probe=True, load_age_s=9.0, pid=42), S.READY, "ours", None),
    ("answering long after the deadline",
     obs(alive=True, launched=True, was_ready=True, probe=True, load_age_s=9999.0, pid=42),
     S.READY, "ours", None),
    ("was ready, process alive, no longer answers",
     obs(alive=True, launched=True, was_ready=True, load_age_s=60.0, pid=42),
     S.FAILED, "ours", "stopped answering"),
    ("a confirmed backend binary holds the port",
     obs(port_held=True, probe=True, pid=111, holder_confirmed=True), S.ADOPTED, "external", None),
    ("a confirmed backend binary holds the port but does not answer",
     obs(port_held=True, pid=111, holder_confirmed=True), S.ADOPTED, "external", None),
    ("process exited while loading",
     obs(launched=True, exit_code=3, load_age_s=1.0),
     S.FAILED, "none", "process exited during load (exit code 3)"),
    ("process exited after it was ready",
     obs(launched=True, was_ready=True, exit_code=-9, load_age_s=500.0),
     S.FAILED, "none", "process exited after it was ready (exit code -9)"),
    ("adopted process died, exit code never seen",
     obs(launched=True, load_age_s=70.0), S.FAILED, "none", "process exited during load"),
    ("our process died and a stranger took the port",
     obs(launched=True, exit_code=1, port_held=True, probe=True, pid=111, holder_confirmed=True),
     S.ADOPTED, "external", None),
    # unknown: a fact could not be gathered, or the facts contradict
    ("port snapshot failed, nothing answers",
     obs(port_held=None), S.UNKNOWN, "none", "listening-port snapshot failed"),
    ("port snapshot failed, probe not run",
     obs(port_held=None, probe=None), S.UNKNOWN, "none", "listening-port snapshot failed"),
    ("port snapshot failed, something answers",
     obs(port_held=None, probe=True), S.UNKNOWN, "none", "listening-port snapshot failed"),
    ("an unrelated program holds the port and answers",
     obs(port_held=True, probe=True, pid=111, holder_confirmed=False),
     S.UNKNOWN, "none", "not this model's server binary"),
    ("an unrelated program holds the port",
     obs(port_held=True, pid=111, holder_confirmed=False),
     S.UNKNOWN, "none", "not this model's server binary"),
    ("the port holder's identity could not be read",
     obs(port_held=True, probe=True, pid=111, holder_confirmed=None),
     S.UNKNOWN, "none", "identity could not be confirmed"),
    ("process alive, probe could not be run",
     obs(alive=True, launched=True, probe=None, load_age_s=3.0, pid=42),
     S.UNKNOWN, "ours", "readiness probe could not be run"),
    ("process alive with no launch record, not answering",
     obs(alive=True, pid=42), S.UNKNOWN, "ours", "no launch record"),
    ("answers its probe but no process holds the port (ready with no process)",
     obs(probe=True), S.UNKNOWN, "none", "no process holds the port"),
]


@pytest.mark.parametrize(
    ("observation", "state", "ownership", "reason"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_derive_table(observation, state, ownership, reason):
    got = derive(observation)
    assert got.state is state
    assert got.ownership == ownership
    if reason is None:
        assert got.reason is None
    else:
        assert got.reason is not None and reason in got.reason


def test_a_dead_process_is_never_loading_or_ready():
    # state-machine-proof: serving
    """A launch whose process is gone reads failed, never loading or ready."""
    got = derive(obs(launched=True, load_age_s=1.0, probe=False))
    assert got.state is S.FAILED
    got = derive(obs(launched=True, load_age_s=1.0, probe=True))
    assert got.state not in (S.LOADING, S.READY)


def test_the_deadline_is_an_argument():
    # state-machine-proof: serving
    observation = obs(alive=True, launched=True, load_age_s=10.0, pid=1)
    assert derive(observation).state is S.LOADING
    assert derive(observation, deadline_s=5.0).state is S.FAILED


def test_derive_refuses_every_illegal_combination():
    # state-machine-proof: serving
    """No observation in the whole space reaches a state its facts forbid."""
    space = itertools.product(
        (False, True),            # alive
        (None, 1),                # exit_code
        (False, True),            # launched
        (False, True),            # was_ready
        (None, 1.0, 999.0),       # load_age_s
        (None, False, True),      # probe
        (None, False, True),      # port_held
        (None, False, True),      # holder_confirmed
    )
    seen = set()
    for alive, exit_code, launched, was_ready, age, probe, held, confirmed in space:
        o = obs(alive=alive, exit_code=exit_code, launched=launched,
                was_ready=was_ready, load_age_s=age, probe=probe, port_held=held,
                holder_confirmed=confirmed)
        got = derive(o)
        seen.add(got.state)
        if got.state is S.READY:
            assert alive and probe is True, o
        if got.state is S.LOADING:
            assert alive and launched and probe is False, o
        if got.state is S.STOPPED:
            assert not alive and not launched and held is False and probe is not True, o
        if got.state is S.ADOPTED:
            assert not alive and held and confirmed is True, o
        if got.state is S.FAILED and not alive:
            assert launched and not held, o
        # failed and unknown always say why; no other state carries a reason.
        assert (got.reason is not None) == (got.state in (S.FAILED, S.UNKNOWN)), o
        assert (got.ownership == "ours") == alive, o
    assert seen == set(S), "the sweep must reach every declared state"
