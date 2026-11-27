# Install, update and migrate Local LLM Hub Lite

Local LLM Hub Lite installs, updates and migrates the way any app in the toolbox does: clone it and run one `setup.bat`. An update is a pull plus a restart. A migration, onto a fresh clone on the same PC or on another one, is one `setup.bat` call. None of them ever resets or overwrites what a PC has configured.

## Install

You need:

- Windows 10 or 11, `git`, and Python 3.12 or newer (the `py` launcher, or `python` on `PATH`).
- Disk and bandwidth for the backends: `python -m src.install --fix` downloads the llama.cpp and whisper.cpp binaries into `vendor/` and the model weights into `models/` (several GB).
- A GPU is optional. `src.install` reports what it finds.
- Node.js is only for the pre-ship gate, never to run the hub.

```powershell
git clone https://github.com/ferraroroberto/local-llm-hub-lite
cd local-llm-hub-lite
.\setup.bat
.\.venv\Scripts\python.exe -m src.install --fix
.\tray.bat
```

`setup.bat` runs `src/installer.py`, which:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is kept inside `.venv`);
3. creates `config/webapp_config.json` from `config/webapp_config.sample.json` when it is missing;
4. ends with two lists: the required items, each `ok`, `missing` or `unknown`, and the optional features, each `available`, `off` or `unknown` with what it unlocks.

Setup has succeeded when it ends with `Setup complete` and exits 0; an optional feature that is `off` never changes that. The optional features, each needed only for what it unlocks:

- **llama.cpp / whisper.cpp binaries and model weights:** `.\.venv\Scripts\python.exe -m src.install --fix` downloads them. It is `off` until they are there, and `unknown` on a first `-WhatIf` run because the venv does not exist yet.
- **Admin sign-in password:** optional, only for callers that are not on this PC. Set it with `.\.venv\Scripts\python.exe -m src.password set` (it prompts twice and stores a salted hash in `config/app_password.json`). The item is `available` when a usable hash is stored or the old `auth_password` in `config/webapp_config.json` is set, `off` when there is neither (an empty or corrupt password file included), and `unknown` when a file it needs cannot be read or the venv does not exist yet.
- **Foundry Local runtime** (Nemotron chat and ASR): `off` until this PC opts into the Nemotron roster with `.\.venv\Scripts\python.exe -m src.foundry setup` (installs the pinned SDK extra, fetches and verifies the exact ASR variant, writes the machine-local `config/models.local.yaml`); a PC that skips it keeps Qwen and Whisper. [foundry-local.md](foundry-local.md) has the details.

The bearer token for non-loopback callers is minted in `config/webapp_config.json` the first time the tray boots; nothing to do.

`.\setup.bat -WhatIf` lists every change it would make and makes none. This app has no log-on autostart of its own, so `-Startup` does nothing here; to start the hub at log on, put a shortcut to `tray.bat` in the Startup folder (`shell:startup`).

The hub serves <http://127.0.0.1:8020/admin/> once `tray.bat` has started it; the port is `hub.port` in `config/models.yaml`.

### Uninstall

1. Quit the tray icon from its menu; that stops the hub and its backends.
2. Remove the `tray.bat` shortcut from `shell:startup`, if you added one.
3. Delete the usage log folder `C:\sqlite\local-llm-hub-lite\` if you do not want the history.
4. Delete the repo folder. Keep a copy of the files listed under "Migrate" first if you may want them back.

The app registers nothing outside its folder and `C:\sqlite\local-llm-hub-lite\`: no service, scheduled task, registry key or hook in another tool.

## Update

```powershell
git pull --ff-only
.\setup.bat
.\tray.bat --restart
```

No tracked file is rewritten at runtime, so the pull always fast-forwards. `setup.bat` on a configured PC is safe, and it is how a new setting arrives:

- Settings file (`config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`webapp_config.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. A file that does not parse is left alone and reported, and the run exits 1.
- `config/models.yaml` is tracked, so `git pull` brings the registry's new rows. It is not an install config: `setup.bat` never touches it. A local edit of it (for example another `hosts.local.enabled` list) makes the pull stop; commit or stash that edit first, then re-apply it after the pull.
- `config/app_password.json` has no sample and is never written by `setup.bat`.
- A second run reports `unchanged` and writes nothing.

## Migrate

What moves is declared in `[install.migrate]` of `.fleet.toml`. The installer does the whole move; nothing here or in `setup.bat` deletes the old copy.

| Copied | What it is |
| --- | --- |
| `config/webapp_config.json` | the bearer token, legacy login password, extra allowed IPs, CORS origins and Telegram alert settings (compared by hash as a secret) |
| `config/app_password.json` | the admin sign-in hash, when one was set (compared by hash as a secret) |
| `config/models.local.yaml` | this PC's roster overlay, Nemotron included, when the old copy had one |
| `vendor/`, `models/` | the llama.cpp / whisper.cpp binaries and model weights, so nothing is downloaded again; it needs several GB of free disk |

The usage database (`usage.sqlite3` in `C:\sqlite\local-llm-hub-lite\`) lives outside the repo folder and is shared: it stays where it is and the new copy finds it unchanged. Logs (`data/logs/`, `webapp/auth.log`, `tray-crash.log`) are history and are not copied.

**Reinstall in the same folder (the usual case).** It works whatever version the installed copy is, because it runs from a fresh clone and none of the old copy's code runs. Run these in order in a PowerShell window from `<root>`, the folder that holds `local-llm-hub-lite`, never from inside the app:

```powershell
cd <root>
git clone https://github.com/ferraroroberto/local-llm-hub-lite local-llm-hub-lite.incoming   # run nothing in it yet
.\local-llm-hub-lite.incoming\setup.bat -Replace local-llm-hub-lite -WhatIf   # read the backup name, every step and every file; nothing changes
.\local-llm-hub-lite.incoming\setup.bat -Replace local-llm-hub-lite
```

It stops the hub and its backends, renames the installed copy to `local-llm-hub-lite.bak-YYYYMMDD-N` (uncommitted edits stay in it), moves the fresh clone to the original path, copies the files above and passes only when that clone serves its own commit on the declared port (`port` in `.fleet.toml`), asking http first and https second. The path is unchanged, so a Startup shortcut to `tray.bat` needs no edit.

- The installed copy must be a git checkout with an `origin`, and the clone must come from the same URL: if `git -C local-llm-hub-lite remote get-url origin` prints another form of it (SSH, say), clone that one.
- A run that is refused changes nothing. `local-llm-hub-lite.incoming` stays where it is: correct what the report names and run the same command again.
- On an install whose `setup.bat` already has `-InPlace`, `.\local-llm-hub-lite\setup.bat -InPlace` does the same and makes the clone itself. An older one stops with `unrecognized arguments: -InPlace`; use `-Replace`.

**Beside it (another folder, or an old copy that is not a git clone):**

```powershell
git clone https://github.com/ferraroroberto/local-llm-hub-lite <NEW FOLDER>
cd <NEW FOLDER>
.\setup.bat -MigrateFrom <OLD FOLDER> -StopOld
```

Only you can do these steps:

- **Carry a registry edit.** `config/models.yaml` is tracked, so the new clone brings its own. If the old copy edited it, compare the two from the folder above, carry over only this PC's own choices (the `hosts.local.enabled` list, `hub.port`, backend ports and `args`) and restart the hub. The installer's verify asks the top-level `port` of `.fleet.toml`, so on a PC whose old copy used another `hub.port` it can fail until the edit is carried.

  ```powershell
  git diff --no-index local-llm-hub-lite.bak-YYYYMMDD-N\config\models.yaml local-llm-hub-lite\config\models.yaml
  .\local-llm-hub-lite\tray.bat --restart
  ```

- **Reinstall Foundry Local**, on a PC that opted into Nemotron: the new `.venv` has no SDK extra. It keeps the copied overlay.

  ```powershell
  cd local-llm-hub-lite
  .\.venv\Scripts\python.exe -m src.foundry setup
  cd ..
  ```

- **Move the usage history to another PC**, if you want it: the move above stays on one PC. With both hubs stopped, copy `usage.sqlite3` (with its `-wal` and `-shm` files) into the new PC's empty `C:\sqlite\local-llm-hub-lite\`. `FLEET_DATA_ROOT` or `LOCAL_LLM_HUB_LITE_DATA_DIR` moves that folder; use the same setting on both.
- **Repoint** whatever stores the old path, after a `-MigrateFrom` into another folder, such as the Startup shortcut: the run lists what still points at it.
- **Check a real client.** Send one request from a client that used the old hub; its bearer token must still be accepted, and the Models tab must start a backend.
- **Fall back** if verify fails (the report names the step): quit the new copy from its tray icon, then, from the folder above, put the backup back under the original name and start it; it is untouched.

  ```powershell
  Rename-Item local-llm-hub-lite local-llm-hub-lite.failed
  Rename-Item local-llm-hub-lite.bak-YYYYMMDD-N local-llm-hub-lite
  .\local-llm-hub-lite\tray.bat
  ```

- **Retire the backup** once you are sure the new copy works, by deleting it yourself. An agent working through this playbook asks you before it deletes anything.
