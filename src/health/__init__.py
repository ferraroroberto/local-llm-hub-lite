"""Machine health: a cheap, read-only verdict on ports, commit charge and disk load.

One background tick every ``interval_s`` reads three probes in-process
(psutil and Windows APIs through ctypes; never a spawned tool), judges them
``ok`` / ``watch`` / ``act`` / ``unknown`` and keeps the answer in memory for
``GET /admin/api/machines/health``. Nothing here kills, restarts or closes
anything.

  * ``probes``  — the three readings: memory (commit + RAM), ports, disk load.
  * ``verdict`` — pure rules over the readings; the response shape.
  * ``watch``   — the tick, the per-reading time budget, staleness and alerts.
"""
