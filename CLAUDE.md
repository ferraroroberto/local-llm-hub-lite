# Project Instructions

## This repository

**Local LLM Hub Lite** — single-machine local HTTP hub, a lite fork of [ferraroroberto/local-llm-hub](https://github.com/ferraroroberto/local-llm-hub) for environments with no cloud CLIs and no fleet: **local backends only** (llama.cpp `llama-server` for chat, whisper.cpp `whisper-server` for transcription), routed by `model` name from Anthropic-shape `POST /v1/messages` and OpenAI-shape `POST /v1/chat/completions` on `:8000`, plus `POST /v1/audio/transcriptions`. Admin SPA at `/admin` with exactly three tabs: **Hub · Models · Playground**. Setup, config reference and usage: `README.md`.

**Project specifics:**

- **Stack:** FastAPI + vanilla JS (no framework, no build step). Windows-first (tray via pystray); the hub itself is cross-platform Python 3.12+.
- **No cloud, no fleet — keep it that way.** Do not add subscription-CLI backends, multi-host routing, Docker services, or OTel/Langfuse exports here; those live in the upstream full fork. New model backends are `config/models.yaml` rows (engine `llama-server` or `whisper-server`), not new code paths.
- **Config & secrets:** the model registry is `config/models.yaml` (committed — models, ports, roles, startup policy). Runtime web config + bearer token in `config/webapp_config.json` (gitignored; sample committed; token auto-generated on first tray boot). There is no `.env`.
- **Ports:** hub `:8000` (tray-owned); backend ports come from `config/models.yaml` (defaults: llama-server `:8081`, whisper-server `:8090`) and are owned by the hub's process manager (`src/backend_process.py`), never by the tray.
- **Auth model:** loopback callers are trusted; non-loopback callers need the bearer token (`Authorization: Bearer` / `x-api-key` / `?token=`); `extra_allowlist` IPs bypass. One implementation in `src/auth_middleware.py`, mounted on both the hub and `/admin`.
- **Layout:** `src/` (hub, registry, process manager, translation, audio proxy, observability ring, system stats, installer), `app_web/` (admin routers + `app_web/static/` SPA), `tray/` (pystray app), `scripts/` (installers + gate + vendored `tray_lifecycle.ps1`), `launchers/` (registry-driven per-model consoles), `tests/` (+ `tests/e2e` Playwright). `vendor/` and `models/` are downloaded by `python -m src.install --fix`, gitignored.
- **Verification:** before declaring any change done, run the gate `powershell -File scripts\verify-before-ship.ps1` (byte-compile `src app_web tray scripts` + static ratchet + docs ratchet + structure ratchet + `pytest -q --ignore=tests/e2e` + `pytest tests/e2e --browser chromium`). The **static ratchet** (`scripts/static_ratchet.py`, vendored; `.fleet.toml [static_ratchet]`, baseline `.static-ratchet-baseline.json`) fails on a duplicate block (jscpd via `npx`, so Node is a prerequisite) or an unused import/variable (ruff `F401,F841`) that is not in the baseline. It reads the whole tree (`vendor/`, `models/` and `_vendored/` excluded), so it is never path-routed, and the gate calls it only through `python -m scripts.static_ratchet_gate`, which fails exit 3 (a detector could not run) as NOT CONFIRMED, never a pass or a skip. Shrink the baseline in the PR that removes debt (`--update-baseline`); adding to it needs `--allow-additions` and shows in review. The **structure ratchet** (`scripts/structure_ratchet.py`, vendored; `.fleet.toml [structure_ratchet]`, baseline `.structure-ratchet-baseline.json`) fails when a new file passes 1,000 lines, a new function 100 lines or a directory 40 modules, when a baselined offender grows past its recorded size, or when the root folders fall outside `shape = "service"` in `.fleet.toml`. The flat `tests/` directory sits at its real size, never excluded, so a new test goes in a subfolder, not the flat directory; where new code goes is `docs/layout.md`. The gate runs the script directly, so exit 3 (not confirmed) fails like a finding. Unit tests need no GPU and no running backends — process/HTTP calls are mocked at client seams.
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
