"""Per-disk load from the Windows performance counters (PDH), through ctypes.

The same ``\\PhysicalDisk(*)`` counters Resource Monitor shows: average
seconds per read and per write, reads and writes per second, ``% Idle Time``
and ``Current Disk Queue Length``. psutil's per-disk times are not used: on
Windows they come back in whole seconds labelled as milliseconds.

A collection reads the kernel's performance data, not the disk. The query
stays open between ticks; each ``collect()`` closes one window and opens the
next. Windows only.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2
# Per-instance statuses that carry a usable value (valid, or valid and new).
_GOOD_CSTATUS = (0x0, 0x1)
_COUNTERS = {
    "read_s": "\\PhysicalDisk(*)\\Avg. Disk sec/Read",
    "write_s": "\\PhysicalDisk(*)\\Avg. Disk sec/Write",
    "reads_per_s": "\\PhysicalDisk(*)\\Disk Reads/sec",
    "writes_per_s": "\\PhysicalDisk(*)\\Disk Writes/sec",
    "idle_percent": "\\PhysicalDisk(*)\\% Idle Time",
    "queue": "\\PhysicalDisk(*)\\Current Disk Queue Length",
}


class _FmtValue(ctypes.Structure):
    # PDH_FMT_COUNTERVALUE with the union read as its double member.
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtValue)]


def _check(status: int, what: str) -> None:
    status &= 0xFFFFFFFF
    if status:
        raise OSError(f"{what} failed: PDH status 0x{status:08X}")


def parse_instance(instance: str) -> Optional[Tuple[int, List[str]]]:
    """``"1 E: F:"`` -> ``(1, ["E:", "F:"])``; ``"_Total"`` -> ``None``."""
    head, *drives = instance.split()
    return (int(head), drives) if head.isdigit() else None


def _ms(seconds: float, ops: float) -> Optional[float]:
    """Average ms per operation, ``None`` when no operation completed in the window."""
    return round(seconds * 1000.0, 3) if ops > 0 else None


class DiskCounters:
    """An open PDH query over every physical disk. One caller at a time."""

    def __init__(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        self._pdh.PdhAddEnglishCounterW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        self._pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        self._pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self._pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        self._query = wintypes.HANDLE()
        _check(self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)), "PdhOpenQuery")
        self._counters: Dict[str, wintypes.HANDLE] = {}
        try:
            for key, path in _COUNTERS.items():
                handle = wintypes.HANDLE()
                # The English name works on any display language.
                _check(self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)),
                       f"PdhAddEnglishCounter {path}")
                self._counters[key] = handle
        except OSError:
            self.close()
            raise
        self._opened = False
        self._drives: Dict[int, List[str]] = {}

    def close(self) -> None:
        if self._query:
            self._pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()

    def _values(self, counter: wintypes.HANDLE) -> Dict[int, float]:
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = self._pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF
        if status != _PDH_MORE_DATA:
            _check(status, "PdhGetFormattedCounterArray (size)")
            return {}
        buf = ctypes.create_string_buffer(size.value)
        _check(self._pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf),
            "PdhGetFormattedCounterArray")
        items = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
        out: Dict[int, float] = {}
        for i in range(count.value):
            parsed = parse_instance(items[i].szName or "")
            if parsed is not None and items[i].FmtValue.CStatus in _GOOD_CSTATUS:
                out[parsed[0]] = float(items[i].FmtValue.doubleValue)
                self._drives[parsed[0]] = parsed[1]
        return out

    def collect(self) -> Optional[List[Dict[str, Any]]]:
        """Close the current window: one row per disk with a usable value in
        every counter, ``None`` on the first call (it only opens a window).
        Raises ``OSError`` when PDH fails."""
        _check(self._pdh.PdhCollectQueryData(self._query), "PdhCollectQueryData")
        if not self._opened:
            self._opened = True
            return None
        values = {key: self._values(handle) for key, handle in self._counters.items()}
        usable = set.intersection(*(set(v) for v in values.values()))
        return [{
            "disk": f"PhysicalDrive{number}",
            "drives": self._drives.get(number, []),
            # The idle counter can overshoot 100 on a window boundary; clamp so busy stays a percentage.
            "busy_percent": round(min(100.0, max(0.0, 100.0 - values["idle_percent"][number])), 1),
            "queue": values["queue"][number],
            "read_ms": _ms(values["read_s"][number], values["reads_per_s"][number]),
            "write_ms": _ms(values["write_s"][number], values["writes_per_s"][number]),
        } for number in sorted(usable)]
