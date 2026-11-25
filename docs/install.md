# Install, update and move Local LLM Hub Lite

Local LLM Hub Lite moves to another Windows PC the way any app in the toolbox does: clone it and run one `setup.bat`. An update is a pull plus a restart. Neither ever resets or overwrites what a PC has configured.

## What you need

- Windows 10 or 11, `git`, and Python 3.12 or newer (the `py` launcher, or `python` on `PATH`).
- Disk and bandwidth for the backends: `python -m src.install --fix` downloads the llama.cpp and whisper.cpp binaries into `vendor/` and the model weights into `models/` (several GB).
- A GPU is optional. `src.install` reports what it finds.
- Node.js is only for the pre-ship gate, never to run the hub.

## Fresh PC

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
4. ends with a checklist that marks each item `ok`, `missing` or `unknown`.

The checklist names what still needs you:

- **llama.cpp / whisper.cpp binaries and model weights:** `.\.venv\Scripts\python.exe -m src.install --fix` downloads them. It is `unknown` on a first `-WhatIf` run because the venv does not exist yet.
- **Admin sign-in password:** optional, only for callers that are not on this PC. Set it with `.\.venv\Scripts\python.exe -m src.password set` (it prompts twice and stores a salted hash in `config/app_password.json`).
- The bearer token for non-loopback callers is minted in `config/webapp_config.json` the first time the tray boots; nothing to do.

`.\setup.bat -WhatIf` lists every change it would make and makes none. This app has no log-on autostart of its own, so `-Startup` does nothing here; to start the hub at log on, put a shortcut to `tray.bat` in the Startup folder (`shell:startup`).

The hub serves <http://127.0.0.1:8020/admin/> once `tray.bat` has started it; the port is `hub.port` in `config/models.yaml`.

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

## Migrate from an existing install

Use this on a PC that already runs an older copy of Local LLM Hub Lite: one that may not be a git clone, and may live at another path. The old copy keeps running until the new one is verified, and it is deleted only when you say so.

1. **Clone fresh beside it.** Clone into a new folder next to the old one (`git clone https://github.com/ferraroroberto/local-llm-hub-lite <NEW FOLDER>`); never clone over the old folder.
2. **Copy this PC's own files across.** From the old folder into the new one, copy only these, keeping their relative paths (all are gitignored except `config/models.yaml`, so nothing tracked is overwritten):
   - configs: `config/webapp_config.json` (the bearer token, the legacy login password, the extra allowed IPs, the CORS origins and the Telegram alert settings) and `config/app_password.json` (the admin sign-in hash, when one was set). Move them as files; never paste their contents into a chat or an issue.
   - the model registry: `config/models.yaml` is tracked, so a copy of an old, edited one would be overwritten by or conflict with the new version. Compare the old file with the new one (`git diff --no-index <OLD FOLDER>\config\models.yaml config\models.yaml`) and carry over only the choices the old copy made on this PC: the `hosts.local.enabled` list, the `hub.port` and any backend port or `args` you changed.
   - the backends and weights: `vendor/` (llama.cpp and whisper.cpp binaries) and `models/` (GGUF and BIN weights). Copying them saves the multi-GB download; skip them and `src.install --fix` downloads them again. Run `.\.venv\Scripts\python.exe -m src.install` afterwards: any binary or weight that is missing or from a layout the new copy does not expect is reported there.
   - state: the usage log, `usage.sqlite3` (with its `-wal` and `-shm` files) in `C:\sqlite\local-llm-hub-lite\`. It lives outside the repo folder, so the new copy at any path finds it again unchanged; copy it only to a PC whose `C:\sqlite\local-llm-hub-lite\` is empty, and stop the old hub first so the file is not mid-write. `FLEET_DATA_ROOT` or `LOCAL_LLM_HUB_LITE_DATA_DIR` moves it elsewhere; use the same setting on the new PC.
   - certificates and secrets: none. This app serves plain HTTP and keeps no certificate, `.env` or key file. Its logs (`data/logs/`, `webapp/auth.log`, `tray-crash.log`) are history, not configuration: leave them behind.
3. **Top them up.** Run `.\setup.bat -WhatIf` in the new folder and read what it would change, then `.\setup.bat`. `config/webapp_config.json` gains only the keys it lacks; the values you copied stay as they were.
4. **Stop the old copy.** Quit its tray icon from the tray menu; that stops the hub and its backends. Then confirm the hub and backend ports from `config/models.yaml` (`:8020`, `:8021` and `:8022` by default) are free: the Hub tab's install checks and `.\.venv\Scripts\python.exe -m src.install` mark a busy port `warn`. Remove the old copy's shortcut in `shell:startup`, if it had one.
5. **Start the new one.** `.\tray.bat`, and put a shortcut to it in `shell:startup` if it should start at log on.
6. **Verify it.** `GET http://127.0.0.1:8020/health` answers 200, `GET /admin/api/version` shows the new checkout's commit as `git_sha`, and the Models tab starts a backend. Send one real request from a client that used the old hub; a client with the old bearer token must still be accepted. If anything is wrong, quit the new tray and start the old one again: it is untouched.
7. **Retire the old folder, only after you confirm.** Nothing in this playbook or in `setup.bat` deletes the old copy. When you are satisfied, rename it first (for example `<OLD FOLDER>.retired`) and run on the new copy for a while; delete it yourself once you are sure. An agent working through this playbook asks you before it deletes anything.

## Uninstall

1. Quit the tray icon from its menu; that stops the hub and its backends.
2. Remove the `tray.bat` shortcut from `shell:startup`, if you added one.
3. Delete the usage log folder `C:\sqlite\local-llm-hub-lite\` if you do not want the history.
4. Delete the repo folder. Keep a copy of the files listed under "Migrate" first (`config/webapp_config.json`, `config/app_password.json`, an edited `config/models.yaml`, and `models/` if the weights took long to download) if you may want them back.

The app registers nothing outside its folder and `C:\sqlite\local-llm-hub-lite\`: no service, scheduled task, registry key or hook in another tool.
