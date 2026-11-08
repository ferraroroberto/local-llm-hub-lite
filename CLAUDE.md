# Project Instructions

## This repository

**Local LLM Hub Lite** — single-machine local HTTP hub, a lite fork of [ferraroroberto/local-llm-hub](https://github.com/ferraroroberto/local-llm-hub) for environments with no cloud CLIs and no fleet: **local backends only** (llama.cpp `llama-server` for chat, whisper.cpp `whisper-server` for transcription), routed by `model` name from Anthropic-shape `POST /v1/messages` and OpenAI-shape `POST /v1/chat/completions` on `:8000`, plus `POST /v1/audio/transcriptions`. Admin SPA at `/admin` with exactly three tabs: **Hub · Models · Playground**. Setup, config reference and usage: `README.md`.

**Project specifics:**

- **Stack:** FastAPI + vanilla JS (no framework, no build step). Windows-first (tray via pystray); the hub itself is cross-platform Python 3.12+.
- **No cloud, no fleet — keep it that way.** Do not add subscription-CLI backends, multi-host routing, Docker services, or OTel/Langfuse exports here; those live in the upstream full fork. New model backends are `config/models.yaml` rows (engine `llama-server` or `whisper-server`), not new code paths.
- **Config & secrets:** the model registry is `config/models.yaml` (committed — models, ports, roles, startup policy). Runtime web config + bearer token in `config/webapp_config.json` (gitignored; sample committed; token auto-generated on first tray boot). There is no `.env`.
- **Ports:** hub `:8000` (tray-owned); backend ports come from `config/models.yaml` (defaults: llama-server `:8081`, whisper-server `:8090`) and are owned by the hub's process manager (`src/process/backend_process.py`), never by the tray.
- **Auth model:** loopback callers are trusted; non-loopback callers need the bearer token (`Authorization: Bearer` / `x-api-key` / `?token=`); `extra_allowlist` IPs bypass. One implementation in `src/auth_middleware.py`, mounted on both the hub and `/admin`.
- **Layout:** `src/` (hub, registry, process manager, translation, audio proxy, observability ring, system stats, installer), `app_web/` (admin routers + `app_web/static/` SPA), `tray/` (pystray app), `scripts/` (installers + gate + vendored `tray_lifecycle.ps1`), `launchers/` (registry-driven per-model consoles), `tests/` (+ `tests/e2e` Playwright). `vendor/` and `models/` are downloaded by `python -m src.install --fix`, gitignored.
- **Verification:** before declaring any change done, run the gate `powershell -File scripts\verify-before-ship.ps1` (byte-compile `src app_web tray scripts` + static, docs, structure, instruction, doc-refs, dead-code and conventions ratchets + `pytest -q --ignore=tests/e2e` + `pytest tests/e2e --browser chromium`). Unit tests need no GPU and no running backends — process/HTTP calls are mocked at client seams. Each ratchet is a vendored `scripts/<name>_ratchet.py` with config `.fleet.toml [<name>_ratchet]` and baseline `.<name>-ratchet-baseline.json`; exit 3 fails as NOT CONFIRMED, never a pass or a skip.
  - **Static:** fails on a duplicate block (jscpd via `npx`, so Node is a prerequisite), an unused import/variable (ruff `F401,F841`) or a curated ruff bug (`B`) / security (`S`, `S113` strict) finding not in the baseline. It reads the whole tree (`vendor/`, `models/`, `_vendored/` excluded), so it is never path-routed, and the gate calls it only through `python -m scripts.static_ratchet_gate`. Shrink the baseline in the PR that removes debt (`--update-baseline`); adding needs `--allow-additions` and shows in review.
  - **Structure:** fails when a new file passes 1,000 lines, a new function 100 lines or a directory 40 modules, when a baselined offender grows past its recorded size, or when the root folders fall outside `shape = "service"` in `.fleet.toml`. The baseline is empty; where new code and new tests go: `docs/layout.md`. The gate runs the script directly, so exit 3 fails like a finding.
  - **Conventions:** four `ast` rules per file: `no-window` (a `subprocess` spawn passes `creationflags`), `logging-not-print` (no `print` in `src/`, `app_web/`, `tray/` library code), `no-drive-path` (no drive-letter literal) and `subprocess-timeout` (a blocking `subprocess.run`/`call`/`check_*` passes `timeout=`, sized to the call with its own error on expiry; only a foreground server takes the exempt marker). A file whose count rises fails; a baselined one may not grow.
- **Restart and verify before hand-off:** code edits do nothing until the `:8000` process restarts. Canonical restart: **`tray.bat --restart`** (orphan-proof reclaim-then-start via the vendored `scripts\tray_lifecycle.ps1`; reclaims only `:8000`, never the model backend ports). Confirm the new build with `GET /admin/api/version` (`git_sha` matches `HEAD`) and `GET /health` → 200.
- **Latest-only model policy:** one model per role. When a newer release covers the same role on the same hardware, replace the row (registry, launcher usage, README table) rather than accumulating entries — old rows survive in git history.

## UX surface

*Design-conformance block — the product is the FastAPI + static SPA under `app_web/static/`.*

- design spec applies: yes
- paths:
  - app_web/static/**/*.css
  - app_web/static/**/*.{js,html}
- key views:                      # single tabbed SPA served at `/admin/`
  - /admin/    (Hub · Models · Playground tabs)

## CI expectations

- Workflow `.github/workflows/e2e.yml` on every PR and push to `main`: byte-compile, unit pytest, Chromium e2e. **Advisory, not required** — the local gate is the contract.
