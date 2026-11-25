"""The machine health tick, its time budget, staleness and act alerts.

One tick every ``interval_s`` reads each probe in a worker thread and keeps
the result; the endpoint answers from that memory only, so a request never
waits on a probe. Each reading has a time budget: one that overruns is
discarded and reads ``unknown`` with how long it took, never its value. A
tick that hangs is never stacked on (the loop awaits it), and once the last
sample is stale the verdict says the tick is still running.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from . import probes, verdict

logger = logging.getLogger(__name__)

INTERVAL_S = 30.0
# Per reading: each probe is one in-process call measured in milliseconds on a
# busy desktop, so two seconds is a stall, not a slow read.
BUDGET_MS = 2000.0
STALE_AFTER_INTERVALS = 3
BOOT_DELAY_S = 10.0
_MAX_ALERTS = 20
_DISK_HISTORY = max(int(line["sustain"]) for line in verdict.DISK_LOAD.values())

Probe = Callable[[], Any]


class HealthWatch:
    """The latest sample and verdict for this machine. The tick thread writes,
    request handlers read; one lock guards both."""

    def __init__(self, probe_map: Optional[Dict[str, Probe]] = None, *,
                 clock: Callable[[], float] = time.time,
                 host_id: Callable[[], str] = lambda: "",
                 interval_s: float = INTERVAL_S, budget_ms: float = BUDGET_MS,
                 stale_after_intervals: int = STALE_AFTER_INTERVALS) -> None:
        self._probes = probe_map
        self._clock = clock
        self._host_id = host_id
        self.interval_s = interval_s
        self._budget_ms = budget_ms
        self._stale_after_s = interval_s * stale_after_intervals
        self._lock = threading.Lock()
        self._sample: Optional[Dict[str, Any]] = None
        self._samples = 0
        self._disk_levels: Dict[str, Deque[Optional[str]]] = {}
        self._alerts: Deque[Dict[str, Any]] = deque(maxlen=_MAX_ALERTS)
        self._in_act = False
        self._running_since: Optional[float] = None

    def _read(self, name: str, probe: Probe) -> Dict[str, Any]:
        start = self._clock()
        try:
            value = probe()
        except probes.Unsupported as exc:
            return {"state": "unsupported", "reason": str(exc), "value": None}
        except probes.Warming as exc:
            return {"state": "warming", "reason": str(exc), "value": None}  # state-literal-exempt: one probe's reading this tick (ok, failed, unsupported, warming, overrun), re-read every tick, never a lifecycle
        except Exception as exc:  # noqa: BLE001 — any failed read is an unknown reading, never a crash
            logger.info("ℹ️ machine health: %s reading failed: %r", name, exc)
            return {"state": "failed", "reason": str(exc) or type(exc).__name__, "value": None}  # state-literal-exempt: one probe's reading this tick (ok, failed, unsupported, warming, overrun), re-read every tick, never a lifecycle
        took_ms = (self._clock() - start) * 1000.0
        if took_ms > self._budget_ms:
            logger.warning("⚠️ machine health: %s reading took %.0f ms, over its %.0f ms budget",
                           name, took_ms, self._budget_ms)
            return {"state": "overrun", "value": None,
                    "reason": f"it took {took_ms:.0f} ms, over its {self._budget_ms:.0f} ms budget"}
        return {"state": "ok", "reason": None, "value": value}

    def mark_running(self) -> None:
        with self._lock:
            self._running_since = self._clock()

    def tick(self) -> Dict[str, Any]:
        """Read every probe once, keep the sample and alert on a move into or
        out of ``act``. Blocking: run it in a worker thread."""
        if self._probes is None:
            self._probes = probes.default_probes()
        self.mark_running()
        start, cpu_start = self._clock(), time.thread_time()
        readings = {name: self._read(name, probe) for name, probe in self._probes.items()}
        sample = {"ts": self._clock(), "readings": readings,
                  "sample_ms": round((self._clock() - start) * 1000.0, 1),
                  "cpu_ms": round((time.thread_time() - cpu_start) * 1000.0, 1)}
        with self._lock:
            self._sample = sample
            self._samples += 1
            self._running_since = None
            seen = set()
            for row in readings["disk"]["value"] or []:
                levels = self._disk_levels.setdefault(row["disk"], deque(maxlen=_DISK_HISTORY))
                levels.append(verdict.disk_level(row))
                seen.add(row["disk"])
            for disk in set(self._disk_levels) - seen:
                del self._disk_levels[disk]
        payload = self.current()
        self._alert_on_transition(payload)
        return payload

    def _alert_on_transition(self, payload: Dict[str, Any]) -> None:
        now = self._clock()
        with self._lock:
            if payload["verdict"] == "act" and not self._in_act:
                self._in_act = True
                self._alerts.append({"ts": now, "kind": "act", "reason": payload["reason"], "fix": payload["fix"]})
                logger.warning("🚨 machine health: act: %s. Suggested: %s", payload["reason"], payload["fix"])
            elif payload["verdict"] != "act" and self._in_act:
                self._in_act = False
                self._alerts.append({"ts": now, "kind": "cleared", "reason": payload["reason"], "fix": None})
                logger.info("✅ machine health: cleared, now %s: %s", payload["verdict"], payload["reason"])

    def _stale_reason(self, sample: Dict[str, Any], running_since: Optional[float]) -> Optional[str]:
        now = self._clock()
        age = now - sample["ts"]
        if age <= self._stale_after_s:
            return None
        if running_since is not None:
            return (f"The last sample is {verdict.fmt_count(age)} s old and the next one is still running "
                    f"after {verdict.fmt_count(now - running_since)} s, so a probe may be stuck")
        return f"The last sample is {verdict.fmt_count(age / 60)} min old"

    def current(self) -> Dict[str, Any]:
        """The verdict payload, from memory only."""
        with self._lock:
            sample, samples, running_since = self._sample, self._samples, self._running_since
            disk_levels: Dict[str, List[Optional[str]]] = {k: list(v) for k, v in self._disk_levels.items()}
            alerts = list(self._alerts)
        if sample is None:
            body = verdict.no_sample(self.interval_s)
        else:
            body = verdict.judge(sample, disk_levels, self.interval_s, samples,
                                 stale_reason=self._stale_reason(sample, running_since))
        return {"host_id": self._host_id(), **body, "enabled": True,
                "interval_s": self.interval_s, "samples": samples, "alerts": alerts}


def _host_id() -> str:
    from ..host_profile import resolve
    try:
        return resolve().id
    except (OSError, RuntimeError, ValueError) as exc:
        logger.info("ℹ️ machine health: host id unreadable: %s", exc)
        return ""


WATCH = HealthWatch(host_id=_host_id)


async def run_loop() -> None:
    """Tick forever on the fixed cadence; a tick that raises is logged, never fatal."""
    from ..request_pools import HEALTH, run_in_pool

    await asyncio.sleep(BOOT_DELAY_S)
    first = True
    while True:
        try:
            payload = await run_in_pool(HEALTH, WATCH.tick)
            (logger.info if first else logger.debug)(
                "ℹ️ machine health: %s (%s), sample took %s ms, %s ms CPU", payload["verdict"],
                payload["reason"], payload["sample_ms"], payload["ports"]["cpu_ms"])
            first = False
        except Exception as exc:  # noqa: BLE001 — the loop must outlive one bad tick
            logger.warning("⚠️ machine health: tick raised: %r", exc)
        await asyncio.sleep(WATCH.interval_s)
