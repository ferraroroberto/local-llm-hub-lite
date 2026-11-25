"""The machine verdict: pure rules over one sample, in local-llm-hub's response shape.

Levels rank ``ok`` < ``unknown`` < ``watch`` < ``act``: a known problem
outranks a blind spot, but a blind spot is never ``ok``. Each reading that
was not taken (failed, unsupported, still warming, or over its time budget)
is an ``unknown`` finding with its reason, and leaves its rows empty rather
than filled with a guess. Thresholds match local-llm-hub's defaults so both
hubs judge a machine the same way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

LEVEL_RANK = {"ok": 0, "unknown": 1, "watch": 2, "act": 3}

# metric -> (label, unit suffix, watch, act, below)
SYSTEM_RULES = {
    "commit_percent": ("Commit charge", "% of limit", 85.0, 95.0, False),
    "ram_available_percent": ("Available RAM", "%", 10.0, 5.0, True),
    "tcp_time_wait": ("TCP time wait", " sockets", 4000.0, 10000.0, False),
}
PORT_RULE = (50.0, 80.0)
# A disk level needs busy, queue and latency past their lines together, for
# this many consecutive samples (one sample per tick).
DISK_LOAD = {
    "watch": {"busy": 90.0, "queue": 4.0, "ms": 30.0, "sustain": 3},
    "alert": {"busy": 95.0, "queue": 8.0, "ms": 100.0, "sustain": 5},
}

_FIXES = {
    "commit_percent": "Close or restart the largest memory users; reboot if no single process holds most of it",
    "ram_available_percent": "Close or restart the largest memory users",
    "tcp_time_wait": "No reboot: sockets in time wait drain in minutes; find the client opening connections in a burst",
    "ports": "No reboot: ports drain in minutes once the burst stops; find the process holding the most ports",
    "disk_load": "Stop or move the heavy disk work on this drive; Resource Monitor's Disk tab names the process",
}
# The top-level coverage key each probe fills (upstream's names: the port table is "tcp").
COVERAGE_KEY = {"memory": "memory", "ports": "tcp", "disk": "disk"}
_DISK_FINDING_LEVEL = {"alert": "act", "watch": "watch", "unknown": "unknown"}


def fmt_count(n: float) -> str:
    """3.9 M / 90 k / 812 / 4.5 — short enough for a one-line reason."""
    a = abs(n)
    if a >= 1e6:
        return f"{n / 1e6:.1f} M"
    if a >= 1e4:
        return f"{n / 1e3:.0f} k"
    if a >= 100:
        return f"{n:.0f}"
    return f"{n:.1f}".rstrip("0").rstrip(".")


def level_of(value: float, watch: float, act: float, below: bool = False) -> str:
    def past(limit: float) -> bool:
        return value <= limit if below else value >= limit
    return "act" if past(act) else "watch" if past(watch) else "ok"


def unknown_finding(metric: str, reason: str) -> Dict[str, Any]:
    return {"level": "unknown", "kind": "probe", "metric": metric, "value": None,
            "process": None, "reason": reason, "fix": None}


def reading_reason(name: str, reading: Dict[str, Any]) -> str:
    """Why a reading that was not taken is not confirmed fine."""
    return f"The {name} reading is {reading['state']}: {reading['reason']}, so it is not confirmed fine"


def disk_level(row: Dict[str, Any]) -> Optional[str]:
    """``ok`` / ``watch`` / ``alert`` for one window of one disk, ``None`` when unreadable.
    Requests queued on a busy disk with none completing is the worst stall, so a
    window with no latency counts as slow."""
    if row.get("busy_percent") is None or row.get("queue") is None:
        return None
    latencies = [v for v in (row.get("read_ms"), row.get("write_ms")) if v is not None]
    ms = max(latencies) if latencies else None
    for name in ("alert", "watch"):
        line = DISK_LOAD[name]
        slow = ms is None or ms >= line["ms"]
        if row["busy_percent"] >= line["busy"] and row["queue"] >= line["queue"] and slow:
            return name
    return "ok"


def disk_state(levels: Sequence[Optional[str]]) -> str:
    """A disk's state from its newest per-sample levels (oldest first)."""
    if not levels or levels[-1] is None:
        return "unknown"
    for name in ("alert", "watch"):
        need = int(DISK_LOAD[name]["sustain"])
        tail = levels[-need:]
        if len(tail) >= need and all(v in ("alert", name) for v in tail):
            return name
    return "ok"


def _system_findings(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for metric, (label, unit, watch, act, below) in SYSTEM_RULES.items():
        value = system.get(metric)
        if value is None:
            continue
        level = level_of(float(value), watch, act, below)
        if level != "ok":
            out.append({"level": level, "kind": "system", "metric": metric, "value": value, "process": None,
                        "reason": f"{label} {fmt_count(float(value))}{unit}", "fix": _FIXES[metric]})
    return out


def _port_findings(ports: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    holder = (ports.get("top_holders") or [{}])[0].get("name")
    for proto in ("tcp", "udp"):
        percent = ports[proto]["percent"]
        level = level_of(float(percent), *PORT_RULE)
        if level != "ok":
            reason = f"{proto.upper()} ephemeral ports {fmt_count(float(percent))}% in use"
            out.append({"level": level, "kind": "system", "metric": f"{proto}_percent", "value": percent,
                        "process": holder, "reason": reason + (f", most held by {holder}" if holder else ""),
                        "fix": _FIXES["ports"]})
    return out


def _disk_findings(disks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in disks:
        level = _DISK_FINDING_LEVEL.get(row["state"])
        if level is None:
            continue
        drives = ", ".join(row["drives"]) or row["disk"]
        reason = (f"{drives} disk load could not be read" if level == "unknown" else
                  f"{drives} disk {fmt_count(row['busy_percent'])}% busy, queue {fmt_count(row['queue'])}")
        out.append({"level": level, "kind": "disk", "metric": "disk_load", "value": row["busy_percent"],
                    "process": None, "reason": reason, "fix": _FIXES["disk_load"] if level != "unknown" else None})
    return out


def summarize(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``verdict``, ``reason``, ``fix`` and the worst-first ``findings``."""
    findings = sorted(findings, key=lambda f: LEVEL_RANK[f["level"]], reverse=True)
    verdict = findings[0]["level"] if findings else "ok"
    if verdict == "ok":
        return {"verdict": "ok", "reason": "No pressure found", "fix": None, "findings": findings}
    worst = findings[0]
    reason = worst["reason"] + (f" (+{len(findings) - 1} more)" if len(findings) > 1 else "")
    fix = next((f["fix"] for f in findings if f["level"] == verdict and f["fix"]), None)
    return {"verdict": verdict, "reason": reason, "fix": fix, "findings": findings}


def judge(sample: Dict[str, Any], disk_levels: Dict[str, List[Optional[str]]], interval_s: float,
          samples: int, stale_reason: Optional[str] = None) -> Dict[str, Any]:
    """The verdict payload (without ``host_id``, ``enabled`` and ``alerts``) for one sample.

    ``stale_reason`` set means the sample is too old to vouch for now: every
    row reads as not measured and the verdict is ``unknown`` with that reason."""
    readings = sample["readings"]
    coverage = {COVERAGE_KEY[name]: reading["state"] for name, reading in readings.items()}
    findings = [unknown_finding(name, reading_reason(name, reading))
                for name, reading in sorted(readings.items()) if reading["state"] != "ok"]

    memory = readings["memory"]["value"] if readings["memory"]["state"] == "ok" else None
    ports = readings["ports"]["value"] if readings["ports"]["state"] == "ok" else None
    disk_rows = readings["disk"]["value"] if readings["disk"]["state"] == "ok" else []
    if stale_reason:
        ports, disk_rows = None, []
        findings = [unknown_finding("sample", stale_reason)]

    system: Dict[str, Any] = dict(memory or {})
    if ports is not None:
        system.update(tcp_time_wait=ports["tcp"]["time_wait"], ephemeral_ports_used=ports["tcp"]["used"],
                      ephemeral_ports_total=ports["tcp"]["total"], ephemeral_ports_percent=ports["tcp"]["percent"])
    disks = [{**row, "state": disk_state(disk_levels.get(row["disk"], []))} for row in disk_rows]
    if not stale_reason:
        findings += _system_findings(system) + (_port_findings(ports) if ports else []) + _disk_findings(disks)

    holders = ports["top_holders"] if ports else []
    out = summarize(findings)
    out.update(
        offenders={"ports": holders}, system=system, coverage=coverage,
        sampled_at=sample["ts"], sample_ms=sample["sample_ms"],
        ports={"sampled_at": sample["ts"], "sample_ms": sample["sample_ms"], "cpu_ms": sample["cpu_ms"],
               "interval_s": interval_s, "samples": samples, "stale": ports is None,
               "coverage": {proto: readings["ports"]["state"] for proto in ("tcp", "udp")},
               "tcp": ports["tcp"] if ports else None, "udp": ports["udp"] if ports else None,
               "top_holders": holders},
        disks=disks,
    )
    return out


def no_sample(interval_s: float) -> Dict[str, Any]:
    """The payload before the first sample: ``unknown``, every row empty."""
    reason = "No sample yet"
    return {
        **summarize([unknown_finding("sample", reason)]), "reason": reason,
        "offenders": {"ports": []}, "system": {}, "coverage": {}, "sampled_at": None, "sample_ms": None,
        "ports": {"sampled_at": None, "sample_ms": None, "cpu_ms": None, "interval_s": interval_s,
                  "samples": 0, "stale": True, "coverage": {}, "tcp": None, "udp": None, "top_holders": []},
        "disks": [],
    }
