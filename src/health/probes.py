"""The three machine readings, each one in-process call per tick.

The machine this runs on is slow, so no reading spawns a tool (no PowerShell,
WMI, ``netstat`` or ``netsh``): commit charge is one ``GetPerformanceInfo``
call, the port table one ``psutil.net_connections`` call (the OS's own
endpoint tables), and disk load one collection of the Windows performance
counters (PDH) over a query kept open between ticks.

A probe that cannot read raises: :class:`Unsupported` when the platform has no
such counter, :class:`Warming` when it needs a second tick, any other
exception when the read failed. The watch turns each into an ``unknown``
reading with its reason; a probe never returns a guessed value.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import psutil

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3
# Windows' default dynamic port range. Reading the configured range takes
# ``netsh`` (a spawn per read), so the default stands in and the block says so.
DEFAULT_PORT_RANGE = (49152, 65535)
_TOP_HOLDERS = 5


class Unsupported(Exception):
    """This platform has no such reading."""


class Warming(Exception):
    """The reading needs one more tick before it has a value."""


def _windows() -> bool:
    return sys.platform == "win32"


class _PerformanceInfo(ctypes.Structure):
    # PERFORMANCE_INFORMATION (psapi.h); the sizes are in pages.
    _fields_ = [("cb", ctypes.c_uint32)] + [(name, ctypes.c_size_t) for name in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize",
    )] + [("HandleCount", ctypes.c_uint32), ("ProcessCount", ctypes.c_uint32),
          ("ThreadCount", ctypes.c_uint32)]


def memory() -> Dict[str, Any]:
    """Commit charge against its limit, and available RAM."""
    if not _windows():
        raise Unsupported("commit charge is a Windows counter (GetPerformanceInfo); not read on this platform")
    info = _PerformanceInfo()
    info.cb = ctypes.sizeof(info)
    if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise OSError(f"GetPerformanceInfo failed (error {ctypes.get_last_error()})")
    page = info.PageSize
    commit, limit = info.CommitTotal * page, info.CommitLimit * page
    total, available = info.PhysicalTotal * page, info.PhysicalAvailable * page
    if not limit or not total:
        raise OSError("GetPerformanceInfo returned a zero commit limit or RAM total")
    return {
        "commit_gb": round(commit / _GIB, 1),
        "commit_limit_gb": round(limit / _GIB, 1),
        "commit_percent": round(100.0 * commit / limit, 1),
        "ram_available_gb": round(available / _GIB, 1),
        "ram_available_percent": round(100.0 * available / total, 1),
    }


Connections = Callable[[], Iterable[Any]]


class PortProbe:
    """Ephemeral ports in use per protocol, TCP TIME_WAIT and the top holders.

    The endpoint table and the name lookup are injectable so a test drives it
    with no socket open. Process names are cached by pid between ticks."""

    def __init__(self, connections: Optional[Connections] = None,
                 name_of: Optional[Callable[[int], str]] = None,
                 port_range: Tuple[int, int] = DEFAULT_PORT_RANGE) -> None:
        self._connections = connections or (lambda: psutil.net_connections(kind="inet"))
        self._name_of = name_of or _process_name
        self._range = port_range
        self._names: Dict[int, str] = {}

    def __call__(self) -> Dict[str, Any]:
        lo, hi = self._range
        used: Dict[str, set] = {"tcp": set(), "udp": set()}
        per_pid: Dict[int, Dict[str, int]] = {}
        time_wait = 0
        for conn in self._connections():
            proto = "tcp" if conn.type == socket.SOCK_STREAM else "udp"
            if proto == "tcp" and conn.status == psutil.CONN_TIME_WAIT:
                time_wait += 1
            port = conn.laddr.port if conn.laddr else None
            if port is None or not lo <= port <= hi:
                continue
            used[proto].add(port)
            if conn.pid:
                counts = per_pid.setdefault(conn.pid, {"tcp": 0, "udp": 0})
                counts[proto] += 1
        total = hi - lo + 1
        out: Dict[str, Any] = {
            proto: {"percent": round(100.0 * len(ports) / total, 1), "used": len(ports),
                    "total": total, "range": [lo, hi]}
            for proto, ports in used.items()
        }
        out["tcp"]["time_wait"] = time_wait
        out["top_holders"] = self._holders(per_pid)
        return out

    def _holders(self, per_pid: Dict[int, Dict[str, int]]) -> List[Dict[str, Any]]:
        top = sorted(per_pid.items(), key=lambda kv: kv[1]["tcp"] + kv[1]["udp"], reverse=True)[:_TOP_HOLDERS]
        self._names = {pid: self._names[pid] for pid, _ in top if pid in self._names}
        rows = []
        for pid, counts in top:
            if pid not in self._names:
                self._names[pid] = self._name_of(pid)
            rows.append({"name": self._names[pid], "pid": pid, "value": counts["tcp"] + counts["udp"],
                         "tcp": counts["tcp"], "udp": counts["udp"], "rate_per_hour": None})
        return rows


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return f"pid {pid}"


class DiskProbe:
    """Per physical disk: busy %, queue length and average ms per read and write.

    Read from the ``\\PhysicalDisk(*)`` performance counters (the ones Resource
    Monitor shows). A collection reads the kernel's performance data, not the
    disk, so it never adds load to what it measures. The first call only opens
    the window and raises :class:`Warming`."""

    def __init__(self) -> None:
        self._counters: Optional[Any] = None

    def __call__(self) -> List[Dict[str, Any]]:
        if not _windows():
            raise Unsupported("disk load is read from Windows performance counters; not read on this platform")
        if self._counters is None:
            from .pdh import DiskCounters
            self._counters = DiskCounters()
        rows = self._counters.collect()
        if rows is None:
            raise Warming("the first disk counter window is still open")
        return rows


def default_probes() -> Dict[str, Callable[[], Any]]:
    """The live probes, keyed by the reading they feed."""
    return {"memory": memory, "ports": PortProbe(), "disk": DiskProbe()}
