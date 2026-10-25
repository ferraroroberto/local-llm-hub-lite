# Repository layout

## Where code goes

The structure ratchet (`scripts/structure_ratchet.py`, configured in `.fleet.toml`) declares this repo as `shape = "service"`: `src/`, `tests/`, `app_web/` and `tray/` are required, `scripts/`, `config/`, `launchers/` and `assets/` are allowed, and `docs/`, `.github/` and hidden folders always are. There is no layer pair because `src/` mounts `app_web/` by design. No folder is declared as an exception: the shape covers every top-level folder here.

| Folder | Holds |
| --- | --- |
| `src/` | The hub: FastAPI app, model registry, backend process manager, Anthropic↔OpenAI translation, audio proxy, observability ring, system stats, installer. The directory is flat; a new module that fits no existing one goes in a subpackage. |
| `app_web/` | The `/admin` sub-app: `routers/` for the API, `static/` for the vanilla-JS SPA (no bundler). Declared by the shape instead of the scaffold's `app/webapp/`. |
| `app_web/static/_vendored/` | Admin-SPA components copied from project-scaffolding. Those declared in `.fleet.toml` `[vendored]` are re-vendored, never edited here; `icons/` is a trimmed local sprite. |
| `tray/` | The pystray tray app that owns the hub process. Declared by the shape instead of the scaffold's `app/tray/`. |
| `tests/` | Unit tests, with `tests/e2e/` for the Playwright suite. The flat directory is close to the 40-module limit, so a new test goes in a subfolder (`tests/ratchets/` holds the gate and ratchet tests). |
| `scripts/` | The gate, the ratchets, installers for llama.cpp / whisper.cpp / weights, the smoke test and the vendored `tray_lifecycle.ps1`. |
| `config/` | The committed model registry (`models.yaml`) and the web-config sample. |
| `launchers/` | Registry-driven per-model consoles (`.bat` and `.sh`). |
| `docs/` | Durable reference. |
| `vendor/`, `models/` | Downloaded llama.cpp / whisper.cpp binaries and weights; gitignored, so outside the ratchet. |

## What the baseline holds

`.structure-ratchet-baseline.json` records every offender at today's size and excludes nothing; it is empty now, so every file, function and directory is under its limit. A baselined entry may not grow; shrinking one is `scripts/structure_ratchet.py --update-baseline`, and raising or adding needs `--allow-additions` and shows in review.
