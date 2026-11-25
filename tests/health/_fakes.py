"""Fake clock and fake probes that drive the health watch with no real reads."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.health import probes

T0 = 1_800_000_000.0


class FakeClock:
    """A wall clock that only moves when a test (or a fake probe) moves it."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def memory(commit_percent: float = 40.0, ram_available_percent: float = 50.0) -> Dict[str, Any]:
    return {"commit_gb": 8.0, "commit_limit_gb": 20.0, "commit_percent": commit_percent,
            "ram_available_gb": 8.0, "ram_available_percent": ram_available_percent}


def ports(tcp_percent: float = 5.0, udp_percent: float = 1.0, time_wait: int = 10) -> Dict[str, Any]:
    total = 16384

    def proto(percent: float) -> Dict[str, Any]:
        return {"percent": percent, "used": round(total * percent / 100), "total": total,
                "range": [49152, 65535]}

    return {"tcp": {**proto(tcp_percent), "time_wait": time_wait}, "udp": proto(udp_percent),
            "top_holders": [{"name": "app.exe", "pid": 101, "value": 40, "tcp": 30, "udp": 10,
                             "rate_per_hour": None}]}


def disk(busy: float = 10.0, queue: float = 0.0, read_ms: Optional[float] = 1.0) -> List[Dict[str, Any]]:
    return [{"disk": "PhysicalDrive0", "drives": ["C:"], "busy_percent": busy, "queue": queue,
             "read_ms": read_ms, "write_ms": read_ms}]


class Fixed:
    """A probe that returns ``value`` (or raises it) and takes ``cost_s`` of fake time."""

    def __init__(self, clock: FakeClock, value: Any, cost_s: float = 0.01) -> None:
        self.clock, self.value, self.cost_s = clock, value, cost_s
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        self.clock.advance(self.cost_s)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


def healthy_probes(clock: FakeClock) -> Dict[str, Fixed]:
    return {"memory": Fixed(clock, memory()), "ports": Fixed(clock, ports()), "disk": Fixed(clock, disk())}


def unsupported(reason: str) -> probes.Unsupported:
    return probes.Unsupported(reason)
