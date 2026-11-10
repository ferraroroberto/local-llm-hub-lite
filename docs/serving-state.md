# Serving state

Each local model backend the hub manages (llama-server, whisper-server) is in exactly one declared **serving state**, derived in one place, [`src/process/serving_state.py`](../src/process/serving_state.py), and read the same way by every surface that shows or acts on it.

## Why it is declared

An *implicit state machine* has real modes (stopped, loading, ready, failed) that are never declared as one official state. They are inferred from scattered facts, so the legal combinations live only in someone's head and two screens can infer different answers from the same facts.

A backend's mode used to be inferred from five facts: the hub's own `Popen` handle, an adopted PID plus its identity, a listening-port snapshot, an HTTP reachability probe and, for on-demand rows, the idle clock. Each reader combined a different subset. A backend still reading weights read "running" on the Models badge and "idle-unloaded" on the startup badge of the same tile; a crash during load was invisible because the tile simply read "stopped"; and a listening-port lookup that failed was folded into "nothing is listening".

Declaring the states makes a bad transition fail loudly (a dead process with a live launch record is `failed`, with its exit code, never `stopped`) and lets a newcomer read the whole lifecycle from one module.

## The states

| State | Meaning | Start | Stop | Ping |
| --- | --- | --- | --- | --- |
| `stopped` | No process of ours, nothing on the port. | yes | no | no |
| `loading` | Ours, spawned or inherited, not yet answering, still inside the load deadline (`LOAD_DEADLINE_S`, 180 s, the same budget an on-demand first request waits). | no | yes | no |
| `ready` | Ours and answering. | no | yes | yes |
| `adopted` | Someone else's process holds the port and is confirmed to be this engine's binary (see [Adoption needs identity](#adoption-needs-identity)); the hub routes to it but did not start it. | no | no (Force stop) | yes |
| `failed` | Ours exited on its own, missed the load deadline, or stopped answering after it was ready. `reason` says which. | yes | yes | no |
| `unknown` | A fact needed to tell the states apart could not be gathered (the listening-port snapshot failed, the readiness probe could not be run, the port holder's identity could not be read), the port holder is some other program, or the facts contradict each other. `reason` says which. | no | no | no |

A `failed` tile enables both Start and Stop because the state does not say whether the process is gone (Start restarts it) or hung (Stop kills it).

## How it is derived

`backend_process.serving(model, reachable, listening)` gathers one `Observation` and `serving_state.derive()` maps it to a state; `derive` is pure. The observation holds:

- whether a process of ours is alive (the `Popen` handle, or an adopted PID whose create time and exe still match), and its exit code once it died;
- the **launch record**: the spawn time and whether the launch has ever answered. `start` and `inherit_running_backends` create it; only a deliberate `stop` drops it. That is what tells "died on its own" (`failed`) from "stopped" (`stopped`);
- the readiness probe, or `None` when it could not be run;
- whether someone listens on the model's port, or `None` when the listening-port lookup failed (`snapshot_listening_pids` returns a `ListeningSnapshot` whose `known` is false in that case);
- whether that port holder is this engine's binary (`holder_confirmed`): `True` confirmed, `False` another program, `None` could not be read.

`unknown` is never folded into a negative: an unknown snapshot is not "nothing listens", and an unrun probe is not "down".

## Adoption needs identity

A listener on a backend's port is `adopted` only when its process exe is the model's engine binary (`llama-server` or `whisper-server`, the same test inheritance uses). A listener that is another program reads `unknown` ("port holder is not this model's server binary"); one whose exe cannot be read, or a port snapshot that failed, reads `unknown` too. `start` follows the same rule: a port that answers but whose holder is not confirmed returns `unconfirmed_listener` and neither adopts nor spawns over it. The Force stop button is offered only for `adopted`; `POST /admin/api/models/{id}/force-stop` still reclaims any port holder when called directly.

## Start and stop outcomes

`bp.start` and `bp.stop` return a [`Result`](../src/process/outcomes.py) whose `outcome` is a declared `Outcome` (`started`, `adopted`, `already_running`, `unknown_model`, `not_launchable`, `launch_failed`, `unconfirmed_listener`, `stopped`, `not_running`, `stop_failed`). `ok` and the operator-facing `message` are derived from it, so a caller branches on the outcome and rewording a message changes no behaviour. `POST /admin/api/models/{id}/start|stop` returns `outcome` beside `ok` and `detail` (HTTP 409 when `ok` is false), and the tray reads that field instead of the text.

## Shutdown leaves adopted backends

A hub stops only the backends it spawned (`bp.spawned_backends()`) when it shuts down. A backend it inherited from a previous hub or adopted from a launcher keeps running; the explicit Stop button and the on-demand idle unload still stop an inherited one, because those are deliberate acts on a tile the hub manages.

## Who reads it

- `GET /admin/api/models` adds `state` and `reason` to each controllable row. The older `ownership`, `pid` and `reachable` fields stay, derived from the same observation.
- The Models tab ([`app_web/static/serving_state.js`](../app_web/static/serving_state.js)) builds the badge, the Start / Stop / Ping enablement, the "Active only" filter and the on-demand badge from `state`. A row from an older hub, which has no `state`, falls back to its `ownership`.
- `GET /v1/audio/health` reports the whisper backend's `state` and `reason` beside `reachable`; the `ok` / `degraded` verdict still follows `reachable`.
- `on_demand.ensure_ready` raises its not-ready error at once, with the reason, when the spawned process turns `failed`, instead of waiting out the deadline.

A new reader calls `backend_process.serving()`; it never re-derives a mode from the process handle, the port or the probe.

## Not covered

The tray's own hub state and the on-demand idle clock are separate machines. Adoption confirms the holder's binary, not which model it loaded: a `llama-server` of a different model on the port reads `adopted`.

## Proof

`tests/process/test_serving_state.py` has one row per reachable observation, the impossible combinations (ready with no process, loading with a dead process, a probe that was never run) and every `unknown` case, plus a sweep of the whole observation space. `tests/process/test_backend_serving.py` covers the launch record, a deliberate stop and the failed snapshot. `tests/process/test_adopted_identity.py` uses stub listeners and stub processes only: an unrelated listener reads `unknown`, `start` declines it, outcomes are read from the declared value, and shutdown leaves an inherited backend running. The tests carry the `state-machine-proof: serving` marker the scaffold's scoreboard counts.
