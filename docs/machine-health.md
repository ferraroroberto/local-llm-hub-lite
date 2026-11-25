# Machine health

`GET /admin/api/machines/health` answers whether this computer is under pressure: `ok`, `watch`, `act`, or `unknown`, with a one-line reason, a suggested fix and the rows behind it. A launcher on the same computer reads it over loopback the way app-launcher reads local-llm-hub's endpoint of the same name, so the response keeps that shape (`tests/fixtures/upstream_machine_health.json` is the contract). It is read-only: nothing here kills, restarts or closes anything.

## What it measures

One tick every 30 s (`src/health/watch.py`), on its own `health` thread pool, reads three probes in-process (`src/health/probes.py`):

| Reading | Source | Rows |
| --- | --- | --- |
| `memory` | `GetPerformanceInfo` via ctypes | `system.commit_*`, `system.ram_available_*` |
| `tcp` (the port table) | `psutil.net_connections` | `ports.tcp`, `ports.udp`, `ports.top_holders`, `system.tcp_time_wait`, `system.ephemeral_ports_*` |
| `disk` | `\PhysicalDisk(*)` performance counters via PDH and ctypes (`src/health/pdh.py`) | `disks[]`: busy %, queue, ms per read and write |

No probe spawns a process: no PowerShell, WMI, `netstat` or `netsh`. The computer this runs on is slow, so a timer-driven spawn is the cost this design avoids. One consequence: the dynamic port range is taken as Windows' default 49152-65535 (`ports.tcp.range`) because reading a changed range needs `netsh`.

On a desktop with about 900 sockets open a tick takes about 10 ms and at most one 15.6 ms Windows CPU tick, about 0.05% of one core at the 30 s cadence (the first tick, which opens the PDH query, about 330 ms). The budget is 0.1% of one core: 30 ms of CPU per tick. `ports.cpu_ms` and `sample_ms` report each tick's actual cost.

## Verdict rules

Thresholds match local-llm-hub's defaults (`src/health/verdict.py`): commit charge watch 85% / act 95%; available RAM watch below 10% / act below 5%; TCP time wait watch 4,000 / act 10,000 sockets; TCP or UDP ephemeral ports in use watch 50% / act 80%; a disk is `watch` after 3 consecutive ticks at least 90% busy with a queue of 4 and 30 ms per operation, `alert` (an `act` finding) after 5 at 95%, 8 and 100 ms. A move into or out of `act` is logged once and kept in `alerts`.

## Unknown is its own state

A reading that was not taken is never shown as healthy. Each probe reports one state in `coverage`: `ok`, `failed` (it raised; the reason carries the error), `unsupported` (no such counter on this platform: commit and disk off Windows), `warming` (the disk counters need a second tick) or `overrun` (it took longer than its 2,000 ms budget; its value is discarded). Any state but `ok` adds an `unknown` finding with that reason and leaves its rows empty (`ports.stale: true`, `ports.tcp: null`, no `disks`).

The endpoint answers from memory only, so a slow probe can never stall a request. A tick is never started on top of a running one; once the last sample is older than three intervals the verdict turns `unknown`, saying the next tick is still running when it is.
