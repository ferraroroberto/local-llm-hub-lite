"""The declared outcome of a backend ``start`` or ``stop``.

A caller branches on :class:`Outcome`, never on the wording of a message. The
message is derived from the outcome (and one detail string for the outcomes
that carry a fact), so rewording it changes nothing a caller does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    """What ``backend_process.start`` / ``stop`` did. Values travel as ``outcome``."""

    STARTED = "started"                        # we spawned it
    ADOPTED = "adopted"                        # a confirmed backend already answers on the port
    ALREADY_RUNNING = "already_running"        # a process of ours exists (maybe still loading)
    UNKNOWN_MODEL = "unknown_model"            # not enabled on this host
    NOT_LAUNCHABLE = "not_launchable"          # binary or weights missing
    LAUNCH_FAILED = "launch_failed"            # the spawn itself raised
    UNCONFIRMED_LISTENER = "unconfirmed_listener"  # the port answers but is not a confirmed backend
    STOPPED = "stopped"
    NOT_RUNNING = "not_running"                # nothing of ours to stop
    STOP_FAILED = "stop_failed"


SUCCESS = frozenset({Outcome.STARTED, Outcome.ADOPTED, Outcome.STOPPED})


@dataclass(frozen=True)
class Result:
    """An outcome plus the one fact its message needs.

    ``detail`` is the fact for outcomes that carry one (the missing file, the
    spawn error, the kill report); ``pid`` is the process spawned or adopted.
    """

    outcome: Outcome
    detail: str = ""
    pid: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.outcome in SUCCESS

    @property
    def message(self) -> str:
        """Operator-facing text, derived from the outcome."""
        o = self.outcome
        pid = f" (PID {self.pid})" if self.pid else ""
        if o is Outcome.STARTED:
            return f"started (pid={self.pid})"
        if o is Outcome.ADOPTED:
            return f"adopted external instance{pid}"
        if o is Outcome.ALREADY_RUNNING:
            return "already running"
        if o is Outcome.UNKNOWN_MODEL:
            return f"model {self.detail!r} not enabled on this host"
        if o is Outcome.NOT_RUNNING:
            return "not running"
        if o is Outcome.STOPPED:
            return self.detail or "stopped"
        return self.detail or o.value.replace("_", " ")
