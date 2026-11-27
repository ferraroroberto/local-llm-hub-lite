# Foundry Local: Nemotron chat and ASR

An optional model roster for a PC that opts in: **Nemotron 3 Nano 4B** for chat on `llama-server` (`:8023`) and **Nemotron 3.5 ASR** for transcription on Microsoft Foundry Local (`:8090`), with Whisper kept as the fallback ASR model. A PC that never opts in keeps the shipped Qwen + Whisper defaults and needs nothing from this page.

## Opt in

```powershell
.\.venv\Scripts\python.exe -m src.foundry setup
.\.venv\Scripts\python.exe -m src.restart_check restart --port 8020 --version-path /admin/api/version
```

`setup` does, in order:

1. installs `requirements-foundry.txt` (the pinned SDK below) into `.venv`, skipped when that version is already there;
2. downloads the exact `runtime_model` of each `engine: foundry-local` row that the roster or this PC's overlay enables into the [model cache](#model-cache-folder) (skipped when it is already there) and checks that exact variant is cached;
3. copies `config/models.foundry.yaml` to the machine-local overlay `config/models.local.yaml`, unless one already exists (then it is left alone and the roster is merged by hand);
4. runs `python -m src.install --fix`, which downloads the Nemotron chat GGUF and anything else the roster lacks.

`setup --no-enable` does steps 1, 2 and 4 without enabling the roster. `python -m src.foundry status` exits 0 when a `foundry-local` row is enabled, the SDK installed and its variant cached, and 1 otherwise. `setup.bat`'s checklist runs it, so a PC that has not opted in reads `off` with the opt-in hint.

The overlay enables `qwen35_4b`, `nemotron4b`, `nemotron_asr` and `whisper`, points the `audio_transcribe` role (and its alias) at `nemotron_asr`, and leaves every other row as shipped. Edit `config/models.local.yaml` for this PC's own changes (a row's `startup: on_demand` on a small GPU, say); `config/models.yaml` stays as pulled.

## Parakeet TDT (offline, not streaming)

The registry also lists `parakeet_asr`: `parakeet-tdt-0.6b-v2-generic-cpu:1` (display name `parakeet-tdt-0.6b-v2`, 692 MB, English, `CPUExecutionProvider`) on `:8024`. It is an **offline** recognizer, not a streaming one: it takes a whole file and answers once. Nemotron stays the roster's transcription model and Whisper the explicit fallback; neither the shipped defaults nor the roster enable Parakeet, so it runs only where the machine overlay names it.

To transcribe with it, opt in as above, then set these keys in `config/models.local.yaml` (keep its other settings, such as `foundry.model_cache_dir`):

```yaml
hosts:
  local:
    enabled: [qwen35_4b, nemotron4b, nemotron_asr, parakeet_asr, whisper]

models:
  whisper:
    aliases: []
  nemotron_asr:
    aliases: []
  parakeet_asr:
    aliases: ["audio_transcribe"]

roles:
  audio:
    transcribe:
      model_id: parakeet_asr
```

Then run `python -m src.install --fix`, which downloads the variant, and restart the hub. Leave `nemotron_asr` out of `enabled` to save its memory when no client asks for it by id.

A client that checks `:8090/health` (Voice Transcriber, below) does not find Parakeet on `:8024`. To serve it on that port, drop `nemotron_asr` from `enabled` and set `models.parakeet_asr.port: 8090`; two rows cannot share a port.

## Model cache folder

By default the SDK keeps models in Lite's own `~/.local-llm-hub-lite/cache/models`. A PC where that folder cannot get the model, but another folder already holds it, names that folder instead. Nothing assumes any other app's cache exists. The first setting found wins:

| Setting | Where |
| --- | --- |
| `LOCAL_LLM_HUB_LITE_FOUNDRY_CACHE_DIR` | environment variable; the hub's backends inherit it, so set it where the tray will see it (a user variable), not only in one shell |
| `foundry.model_cache_dir` | the gitignored `config/models.local.yaml` |
| neither | Lite's own default |

```yaml
foundry:
  model_cache_dir: "~/my-foundry-cache"
```

A relative path is read from the repo root; `~` and environment variables expand. A path that is a file is refused. A folder that does not exist yet is not: the SDK downloads into it, so check the spelling. The hub reads the setting at start, so restart it after a change.

To opt in on a PC that already has such a folder, copy `config/models.foundry.yaml` to `config/models.local.yaml`, set the key there, then run `setup`: it leaves the existing overlay alone and fetches nothing the folder already holds.

The worker's log and `src.foundry status` name the folder in use, and the worker's `/health` reports it as `model_cache`: `path`, `source` (`env`, `config` or `default`) and `state` (`present`, `missing`, `not_a_folder`, or `unknown` when the folder cannot be read).

## Without Foundry Local

Nothing in the hub imports the SDK at load. On a PC without it:

- `setup.bat` installs and serves Qwen and Whisper as always;
- `python -m src.install` and the admin Hub tab show `Foundry Local runtime (optional: Nemotron ASR)` as a warning, `Foundry Local not installed`, naming the opt-in command;
- a `foundry-local` row enabled anyway fails to start with that same message (the Models tile shows it), and its install row is `missing` with a fix that installs the SDK and fetches the variant.

## How an ASR row runs

Both rows are `backend: whisper` (the OpenAI audio shape) and `engine: foundry-local`, each with its exact `runtime_model`. The hub's process manager spawns `python -m src.foundry.worker <row id>` like any backend: same per-backend log, start, stop, adoption across a hub restart. The worker:

- resolves exactly that catalog variant: an alias (no `:version`), an unknown id or a catalog that hands back another variant is refused, with a message naming what was asked and what it resolved to;
- loads it (reusing it when already loaded, downloading it when missing) before it opens the port, so a reachable port means ready;
- serves `GET /health` with `engine` (`foundry-local`), `model` (the exact variant), `model_id`, `sdk_version`, `pid` and [`model_cache`](#model-cache-folder); `GET /v1/models`; and `POST /v1/audio/transcriptions` taking 16-bit PCM WAV and answering `{"text": ...}` (`response_format` `json`, `text` or `verbose_json`; `task=translate` is refused).

The row's `asr_mode` picks the SDK call, because each kind of recognizer rejects the other's:

| `asr_mode` | Row | How the WAV is transcribed |
| --- | --- | --- |
| `streaming` (default) | `nemotron_asr` | Fed through the SDK's live transcription session as PCM chunks; the final segments are returned. File transcription is rejected ("not a registered multi-modal model type"). |
| `offline` | `parakeet_asr` | Written to a temporary file and transcribed whole in one call; the file is removed afterwards. A live session is rejected ("requires a nemotron_speech model type"). |

Any other value refuses the row.

The worker runs under the generic Python interpreter, so the hub recognises it by its command line (the worker module and the row id), not its exe. On Windows it is spawned as the base interpreter with `__PYVENV_LAUNCHER__` set to the venv, so the PID the hub records is the process holding the port rather than the venv launcher's.

## Routing

`POST /v1/audio/transcriptions` with no `model`, the role alias `audio_transcribe` or a name the registry does not know (OpenAI's `whisper-1`) goes to the `roles.audio.transcribe` target when this PC serves it; failing that a `foundry-local` row comes before `whisper-server`. An explicit known id is still served by exactly that row, so `model=whisper` reaches Whisper beside Nemotron. `GET /v1/audio/health` reports the target's `engine` and `model`. Parakeet is reached by its id, or by the role once the overlay names it as the target.

A row declared both `virtual` (an alias of another backend's port) and runtime-backed is refused: the registry leaves it out, so `/v1/models` never lists a name a different model answers, `src.install` shows an error row, and a transcription role pointing at it answers 503 `refused` with the reason instead of falling back to another model.

## Sharing :8090 with Voice Transcriber

Voice Transcriber can run its own whisper.cpp on `:8090`. When the hub's Nemotron ASR owns the port, Voice checks `GET http://127.0.0.1:8090/health` and adopts it only when `engine` and `model` identify an ASR backend; the hub side of that contract is the `/health` above.

## Versions

| Piece | Version | Why |
| --- | --- | --- |
| `foundry-local-sdk` | `1.2.4` (pinned) | The version the earlier working install ran with this exact variant. Its native core ships in the wheel (`foundry-local-core 1.2.4`, `onnxruntime-core 1.26.0`, `onnxruntime-genai-core 0.14.1`), so no Foundry Local CLI or service is needed, and it installs on the hub's Python 3.12 to 3.14. |
| Foundry Local CLI | not required | The SDK runs the model in-process. The `Microsoft.FoundryLocal` winget package (0.14.0) currently fails to download its installer (404), and the hub does not need it. |
| `foundry-local-sdk` 2.x | not used | 2.x is a different in-process API on a different native runtime (`FoundryLocalManager.get_catalog()` in place of `.catalog`, a cffi binding to the C++ SDK). Nothing shows it reads the 1.x model cache or serves this variant the same way, so moving to it is a separate change with its own live proof. |

Verified on a Windows 11 PC with an RTX 5060 Ti: `foundry-local-sdk 1.2.4` on Python 3.14 resolved and downloaded `nemotron-3.5-asr-streaming-0.6b-generic-cpu:3` (756 MB, CPU) and transcribed a 6 s 16 kHz WAV in about 1 s through the worker. The same SDK resolved and downloaded `parakeet-tdt-0.6b-v2-generic-cpu:1` (692 MB, CPU) and transcribed 16 kHz mono and 48 kHz stereo WAVs.
