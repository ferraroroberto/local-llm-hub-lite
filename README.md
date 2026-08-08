# Local LLM Hub Lite 🧠

A tiny local HTTP hub for running **open-weight LLMs and Whisper on your own machine** — a lite, single-machine fork of [ferraroroberto/local-llm-hub](https://github.com/ferraroroberto/local-llm-hub) with **no cloud backends and no external dependencies**: no Claude/Gemini CLIs, no Docker, no multi-host fleet, no telemetry stack. Just:

- **`POST /v1/messages`** (Anthropic shape) and **`POST /v1/chat/completions`** (OpenAI shape, streaming supported) on `:8000`, routed by `model` name to local [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` backends.
- **`POST /v1/audio/transcriptions`** (OpenAI shape) proxied to a local [whisper.cpp](https://github.com/ggerganov/whisper.cpp) `whisper-server`.
- An **admin SPA** at `/admin` — three tabs: **Hub** (health, live requests, counters, errors, log, CPU/GPU/RAM/VRAM stats, install checks with one-click fixes), **Models** (start/stop each backend, roles card), **Playground** (chat + transcribe).
- A **Windows tray icon** that owns the hub lifecycle (start at login, restart, per-model toggles).

Default model lineup (edit `config/models.yaml` to change it):

| Role | Model | Backend | Port |
|---|---|---|---|
| `agentic_light` / `agentic_heavy` | `qwen3.5-4b` (Q4_K_M, ~2.1 GB VRAM) | `llama-server` | `:8081` |
| `audio_transcribe` | `whisper-large-v3-turbo` (~2 GB VRAM) | `whisper-server` | `:8090` |

Models can be `startup: eager` (autostart with the hub) or `startup: on_demand` (first request spawns the backend; optional `idle_unload_minutes` frees the VRAM again).

## Quick start

```bat
git clone https://github.com/ferraroroberto/local-llm-hub-lite
cd local-llm-hub-lite
setup.bat                                      REM .venv + requirements + install report
.venv\Scripts\python.exe -m src.install --fix  REM download llama.cpp + whisper.cpp + weights
tray.bat                                       REM tray icon -> hub on :8000
```

`python -m src.install` reports what's missing (venv, deps, GPU, vendored binaries, model weights, free ports); `--fix` downloads the platform llama.cpp/whisper.cpp release binaries into `vendor/` and the GGUF/BIN weights into `models/` (several GB — grab a coffee). Everything is idempotent; run it again any time. The same checks live in the admin Hub tab with per-item **Fix** buttons.

Open the admin at <http://127.0.0.1:8000/admin/>. `GET /health` returns 200 once the hub is up.

## Calling it

Point any standard SDK at the hub and pick a backend with the `model` string — model ids, role aliases (`agentic_light`, `agentic_heavy`, `audio_transcribe`), and registry keys all resolve:

```python
# Anthropic shape
from anthropic import Anthropic
client = Anthropic(api_key="local-dummy", base_url="http://127.0.0.1:8000")
msg = client.messages.create(
    model="agentic_light",  # or "qwen3.5-4b"
    max_tokens=256,
    messages=[{"role": "user", "content": "Three bullet points on llama.cpp."}],
)
print(msg.content[0].text)
```

```python
# OpenAI shape (streaming works)
from openai import OpenAI
client = OpenAI(api_key="local-dummy", base_url="http://127.0.0.1:8000/v1")
out = client.chat.completions.create(
    model="qwen3.5-4b",
    messages=[{"role": "user", "content": "Say pong."}],
)
print(out.choices[0].message.content)
```

```bash
# Transcription (through the hub -> lands in the observability ring)
curl -F file=@clip.wav http://127.0.0.1:8000/v1/audio/transcriptions
```

`GET /v1/models` lists every enabled model. Local requests never leave the machine.

**Limitations (by design of the lite fork):** local backends are text-only — image/document content blocks are rejected with a 400; no `stream=true` on `/v1/messages` (use the OpenAI shape for streaming); Anthropic-shape tool-use is not implemented for llama backends (OpenAI-shape tool calling works via llama.cpp's `--jinja`).

## The admin tabs

### Hub

Status pill + uptime, the **install checks** card (with Fix buttons), **live requests** (SSE stream of every `/v1/*` call with latency and status), per-model **counters**, recent **errors**, the **hub log** tail, and **sparkline tiles for CPU, GPU utilization, RAM and VRAM** sampled every 2 s (`GET /admin/api/hub/stats`; GPU figures via `nvidia-smi` when present).

### Models

One card per registry row: engine, port, PID, live/reachable state, startup policy (`eager` / `on-demand · loaded` / `on-demand · idle-unloaded`), start/stop/force-stop, a log-tail drawer, and a ping button. The **roles card** shows which model currently fills each role (`agentic_light`, `agentic_heavy`, `audio.transcribe`).

### Playground

A **chat card** (pick any enabled chat model, system prompt + message, response with latency) and a **transcribe card** (upload an audio file, get the whisper transcript back through the hub's own `/v1/audio/transcriptions`).

## Configuration reference

### `config/models.yaml` (committed — the registry)

Single source of truth for models, ports, and roles. Structure:

```yaml
hub:    { port: 8000, bind_host: "0.0.0.0" }
tray:   { autostart_hub: true, hub_ready_timeout_s: 30 }
hosts:
  local: { platform: win32, default: true, enabled: [qwen35_4b, whisper] }
models:
  qwen35_4b:
    display_name: qwen3.5-4b        # the id clients send
    aliases: ["agentic_light"]      # extra ids that resolve here
    backend: openai                 # openai (chat) | whisper (ASR)
    engine: llama-server            # llama-server | whisper-server
    port: 8081
    model_path: "models/Qwen3.5-4B-Q4_K_M.gguf"
    hf_repo: "unsloth/Qwen3.5-4B-GGUF"   # where --fix downloads from
    hf_pattern: "Qwen3.5-4B-Q4_K_M.gguf"
    startup: eager                  # eager | on_demand
    args: [...]                     # extra backend CLI args
roles:  { ... }                     # display card in the Models tab
```

To add a model: add a row, list its id in `hosts.local.enabled`, run `python -m src.install --fix` (or hit Fix in the admin), start it from the Models tab or `launchers\run_model.bat <id>`.

### `config/webapp_config.json` (gitignored; sample committed)

| Key | Default | Effect |
| --- | --- | --- |
| `auth_token` | auto-generated on first tray boot | Bearer token required for **non-loopback** callers (`Authorization: Bearer`, `x-api-key`, or `?token=`). Loopback needs no token. |
| `auth_password` | `""` | Optional login password for the admin SPA (`POST /api/login` swaps it for the token). |
| `extra_allowlist` | `[]` | IPs allowed without a token (e.g. a trusted LAN peer). |
| `cors_allow_origins` | `[]` | CORS origins for browser callers. |

## Layout

```
src/               hub: FastAPI app, model registry, backend process manager,
                   anthropic<->openai translation, audio proxy, observability ring,
                   system stats, installer
app_web/           /admin sub-app: routers + static vanilla-JS SPA (3 tabs, PWA)
config/            models.yaml (registry) + webapp_config.sample.json
scripts/           installers (llama.cpp / whisper.cpp / weights), smoke test,
                   verify-before-ship gate, vendored tray_lifecycle.ps1
launchers/         run_model.bat/.sh <id>, run_all.bat/.sh (registry-driven)
tray/              pystray tray app (owns the hub process)
tests/             pytest suite + Playwright e2e (tests/e2e)
vendor/            llama.cpp + whisper.cpp binaries (downloaded, gitignored)
models/            GGUF/BIN weights (downloaded, gitignored)
```

## Restart & verify

The canonical restart is **`tray.bat --restart`** — an orphan-proof reclaim-then-start (kills the tray subtree, reclaims `:8000` by repo-scoped PID via the vendored `scripts\tray_lifecycle.ps1`, starts fresh, verifies the new build's `git_sha`). It never touches the model backend ports. Confirm with `GET /health` (the `/admin/api/version` payload carries `git_sha`).

Before shipping a change, run the local gate:

```powershell
powershell -File scripts\verify-before-ship.ps1   # byte-compile + pytest + e2e (chromium)
```

CI (`.github/workflows/e2e.yml`) runs the same three steps on every PR/push to `main` — advisory, the local gate is the contract.

## Scope & usage policy

This is a **personal tool** for running open-weight models on hardware you own. The hub binds `0.0.0.0` so your other devices on a trusted network can reach it (token-gated); don't port-forward it to the public internet. Model weights come with their own licenses (Qwen: Apache 2.0; Whisper: MIT) — check before redistributing.
