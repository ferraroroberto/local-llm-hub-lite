# Repository layout

## Where code goes

The structure ratchet (`scripts/structure_ratchet.py`, configured in `.fleet.toml`) declares this repo as `shape = "service"`: `src/`, `tests/`, `app_web/` and `tray/` are required, `scripts/`, `config/`, `launchers/` and `assets/` are allowed, and `docs/`, `.github/` and hidden folders always are. There is no layer pair because `src/` mounts `app_web/` by design. No folder is declared as an exception: the shape covers every top-level folder here. The one declared root file beyond the shape is `setup.bat` (`extra_root_files`), the installer launcher.

| Folder | Holds |
| --- | --- |
| `src/` | The hub: FastAPI app, model registry, backend process manager, Anthropic↔OpenAI translation, audio proxy, observability ring, system stats, the first-run checks (`src/install.py`), the vendored clone-and-setup installer (`src/installer.py`, run by the root `setup.bat`, with `src/installer_migrate.py` and `src/installer_inplace.py` behind `-MigrateFrom` and `-InPlace`; `docs/install.md`), and the run-time write guard (`src/runtime_files.py`). The process manager lives in `src/process/`, the Anthropic error envelopes in `src/routes/`, the ASR proxy in `src/audio/` and the optional Foundry Local runtime and ASR worker in `src/foundry/`, mirroring local-llm-hub's subpackages; a new module that fits no existing one goes in a subpackage. `src/health_routes.py` is the vendored scaffold router for `GET /healthz` and `GET /api/version`, mounted by `src/server.py`. `src/ps_script.py` is the vendored PowerShell launcher: `tray.bat` starts the tray helper through it under any execution policy, without overriding it. |
| `app_web/` | The `/admin` sub-app: `routers/` for the API, `static/` for the vanilla-JS SPA (no bundler). Declared by the shape instead of the scaffold's `app/webapp/`. |
| `app_web/static/_vendored/` | Admin-SPA components copied from project-scaffolding. Those declared in `.fleet.toml` `[vendored]` are re-vendored, never edited here; `icons/` is a trimmed local sprite. |
| `tray/` | The pystray tray app that owns the hub process. Declared by the shape instead of the scaffold's `app/tray/`. |
| `tests/` | Unit tests, with `tests/e2e/` for the Playwright suite. The flat directory is close to the 40-module limit, so a new test goes in a subfolder (`tests/ratchets/` holds the gate and ratchet tests, `tests/e2e_harness/` the tests of the e2e harness itself, `tests/foundry/` the Foundry Local tests and their faked SDK). |
| `scripts/` | The gate, the ratchets, installers for llama.cpp / whisper.cpp / weights, the smoke test and the vendored `tray_lifecycle.ps1`. |
| `config/` | The committed model registry (`models.yaml`), the opt-in Nemotron overlay (`models.foundry.yaml`, copied to the gitignored `models.local.yaml` by `python -m src.foundry setup`) and the web-config sample. |
| `launchers/` | Registry-driven per-model consoles (`.bat` and `.sh`). |
| `docs/` | Durable reference. |
| `vendor/`, `models/` | Downloaded llama.cpp / whisper.cpp binaries and weights; gitignored, so outside the ratchet. |

## What the baseline holds

`.structure-ratchet-baseline.json` records every offender at today's size and excludes nothing; it is empty now, so every file, function and directory is under its limit. A baselined entry may not grow; shrinking one is `scripts/structure_ratchet.py --update-baseline`, and raising or adding needs `--allow-additions` and shows in review.
