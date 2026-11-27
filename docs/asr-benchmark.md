# Speech-to-text benchmark for a 4 GB Pascal GPU

`python -m src.audio.bench` measures every speech-to-text candidate installed on a PC, on the same clips, and writes one Markdown table. It exists to pick the transcription engine for a PC whose only GPU is a Quadro P1000 (4 GB, Pascal, compute capability 6.1). Numbers mean something only for the machine they were measured on, so the table names that machine's CPU, RAM and GPU. Nemotron ASR stays the transcription role's target until a winner passes a live streaming and accuracy check.

## Candidates

| Id | Engine | Device | Kind | Files under the root |
| --- | --- | --- | --- | --- |
| `nemotron-asr` | Foundry Local, the registry's `nemotron_asr` row | CPU | streaming | the Foundry model cache (docs/foundry-local.md) |
| `parakeet-asr` | Foundry Local, the registry's `parakeet_asr` row | CPU | offline | the Foundry model cache |
| `parakeet-unified-{240,560,1120}ms-{cpu,cuda}` | sherpa-onnx, `nvidia/parakeet-unified-en-0.6b` int8 streaming exports | CPU or CUDA | streaming | `models/bench/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-<chunk>/` |
| `whisper-turbo-{f16,q8_0,q5_0}-{cuda,cpu}` | whisper.cpp `whisper-server`, large-v3-turbo | CUDA or CPU (`-ng`) | offline | `vendor/whisper.cpp/`, `models/ggml-large-v3-turbo*.bin` |
| `faster-whisper-turbo-int8-{cuda,cpu}` | faster-whisper (CTranslate2), large-v3-turbo int8 | CUDA or CPU | offline | `models/bench/faster-whisper-large-v3-turbo/` |

What is known about each before any measurement:

- **Foundry rows.** Both registry variants are CPU builds (`-generic-cpu`), so they never touch the GPU. Nemotron is today's default.
- **Parakeet Unified (sherpa-onnx).** The model card is [nvidia/parakeet-unified-en-0.6b](https://huggingface.co/nvidia/parakeet-unified-en-0.6b). sherpa-onnx exports it at three chunk sizes ([export scripts](https://github.com/k2-fsa/sherpa-onnx/blob/master/scripts/nemo/parakeet-unified-en-0.6b/README.md)), published as about 500 MB archives in its [`asr-models` release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models). `OnlineRecognizer` recognises the export from the decoder's `streaming_model_type` metadata, so it loads with the ordinary transducer call. sherpa-onnx 2.0.2 is the first release with the fix to its buffered streaming (its changelog). The CPU wheel on PyPI is newer than that. The newest Windows CUDA wheel, `2.0.1+cuda12.cudnn9` ([wheel index](https://k2-fsa.github.io/sherpa/onnx/cuda.html)), is older, so the CUDA rows run without that fix. That wheel needs the CUDA 12 runtime and cuDNN 9 on `PATH`. Its documentation does not say whether its ONNX Runtime has Pascal kernels. The run shows it: a CUDA row whose GPU memory does not rise is marked `no VRAM rise: ran on the CPU`. The streaming variants trade accuracy for delay: 240 ms is the most responsive, 1120 ms the most accurate. The run starts with 560 ms.
- **whisper.cpp.** The hub installs whisper.cpp's CUDA 12 Windows build (`scripts/install_whisper_cpp.py`). CUDA 12 still supports Pascal (CUDA 13 dropped it). The quantized `q5_0` and `q8_0` weights come from [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp). All three weight files fit in 4 GB with room to spare. The server runs with the registry's `whisper` row arguments, so the measured decoder carries the same anti-hallucination settings the hub serves.
- **faster-whisper.** CTranslate2 runs int8 on compute capability 6.1, but float16 needs 7.0 or newer ([CTranslate2 quantization](https://opennmt.net/CTranslate2/quantization.html)), so int8 is the only GPU type the benchmark uses. The weights are [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo), the repo faster-whisper itself maps `large-v3-turbo` to.

## What the table reports

| Column | Meaning |
| --- | --- |
| Cold load (s) | From spawning the engine to its first ready answer (`/health` 200, or the runner's ready line). Includes reading the weights from disk. |
| Warm RTF | Processing time over audio time, summed over every clip, after one warm-up transcription that is not counted. Below 1 is faster than real time. |
| EOU finalize (s) | Streaming engines run in-process only (sherpa-onnx): the mean time from the end of the audio to the final text, which is the compute part of the end-of-utterance delay. A live stream adds the export's chunk latency on top. `n/a` for offline engines; `—` for the Foundry streaming row, which is measured through its HTTP worker, which does not expose that point. |
| WER | Word error rate against the clips' reference transcripts, after lowercasing and dropping punctuation. Numbers written as digits and as words count as different, so write references the way the engines tend to. |
| CPU (cores) | Mean CPU use of the engine's process tree during transcription, in cores (2.00 is two cores busy). |
| Peak RAM (MB) | Peak resident memory of the engine's process tree, load included. |
| GPU util (%) | Mean whole-GPU utilisation during transcription. |
| VRAM rise (MB) | Peak GPU memory in use minus a reading taken just before the engine started. Windows does not report per-process GPU memory, so anything else using the GPU at the time moves it. |

A candidate that is not installed reads `not installed` with the reason, and one that fails reads `failed` with the error. Neither has a number in any column.

## How it runs

Each candidate runs alone, one after another, so no two engines compete for the CPU or the GPU. Each is a process the benchmark spawns itself on a free loopback port that no registry row uses: `whisper-server`, the Foundry worker (on a scratch copy of this PC's merged registry with only its row enabled, in a temporary folder), or `python -m src.audio.bench.runner` for sherpa-onnx and faster-whisper. The benchmark stops only the processes it started; the hub and its backends keep running. When a live backend holds the GPU, its memory is part of the baseline, and its load can slow the candidate, so stop the hub first for clean GPU numbers.

Clips are 16 kHz mono 16-bit PCM WAV files in one folder, each with an optional reference transcript beside it (`call.wav`, `call.txt`). Other formats are refused, with the ffmpeg command that converts them.

## Run it on the P1000 PC

From the repo root in PowerShell:

```powershell
# Python packages the hub itself never imports: sherpa-onnx's CUDA build and faster-whisper.
.\.venv\Scripts\python.exe -m pip install "sherpa-onnx==2.0.1+cuda12.cudnn9" -f https://k2-fsa.github.io/sherpa/onnx/cuda.html
.\.venv\Scripts\python.exe -m pip install faster-whisper
# The benchmark-only models: whisper q5_0/q8_0, the three Parakeet Unified exports, faster-whisper turbo.
.\.venv\Scripts\python.exe -m src.audio.bench fetch
# What is installed here, and why the rest is not.
.\.venv\Scripts\python.exe -m src.audio.bench list
# The measurement. Point --clips at a folder of representative recordings.
.\.venv\Scripts\python.exe -m src.audio.bench run --clips <clip folder> --out data\asr-bench\table.md --transcripts data\asr-bench\transcripts.md
```

`fetch` takes item names (`whisper`, `sherpa-560ms`, `sherpa-240ms`, `sherpa-1120ms`, `faster-whisper`) to fetch only some; an item already there is skipped, and nothing is deleted. `run --only "parakeet-unified-560ms-*" "whisper-turbo-q5_0-*"` measures a subset; ids accept `*` wildcards. `--root <checkout>` reads `vendor/` and `models/` from another checkout of this repo. The table goes to stdout and `--out`; `--transcripts` keeps each engine's text per clip, which may hold what the recordings said, so it stays on the PC (`data/` is gitignored).

## Results

None recorded yet. The table from the P1000 PC goes here, with the pick. The winner gets its own implementation issue. A native streaming worker must define buffering, partial and final segments, utterance end, language and 16 kHz PCM handling, and keep the `/v1/audio/transcriptions` shape and the Whisper fallback.
