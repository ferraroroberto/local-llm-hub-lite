"""``GET /admin/api/machines/health`` — this machine's health verdict.

Ports in use, commit charge and per-disk load judged ``ok`` / ``watch`` /
``act`` / ``unknown``, in the same shape as local-llm-hub's endpoint so a
launcher reads either hub alike. Answered from the health tick's memory
(``src/health/watch.py``), so it is cheap to poll and never waits on a probe.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from src.health import watch as health_watch

router = APIRouter()


@router.get("/api/machines/health")
async def machines_health() -> Dict[str, Any]:
    return health_watch.WATCH.current()
