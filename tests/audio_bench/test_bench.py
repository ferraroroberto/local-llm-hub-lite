"""The speech-to-text benchmark with fake engines: rows, table, clips, candidates and both session kinds."""

from __future__ import annotations

import shutil
import sys
import textwrap
import wave
from pathlib import Path

import pytest
import yaml

from src import host_profile
from src.audio.bench import candidates as cands
from src.audio.bench import report, runner
from src.audio.bench.clips import ClipError, load_clips, word_errors
from src.audio.bench.measure import FAILED, NOT_INSTALLED, OK, Samples, run_all
from src.audio.bench.sessions import (
    EngineError,
    HttpServerSession,
    RunnerSession,
    Transcript,
)

REPO = Path(__file__).resolve().parents[2]


def _wav(path: Path, seconds: float, rate: int = 16000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate) * channels)
    return path


@pytest.fixture
def clips(tmp_path):
    _wav(tmp_path / "a.wav", 2.0)
    (tmp_path / "a.txt").write_text("Hello there, world.", encoding="utf-8")
    _wav(tmp_path / "b.wav", 2.0)
    return load_clips(tmp_path)


class FakeSession:
    def __init__(self, text="hello there world", fail_on=None):
        self.text, self.fail_on, self.calls, self.stopped = text, fail_on, 0, False

    def start(self):
        return 4242

    def wait_ready(self):
        if self.fail_on == "ready":
            raise EngineError("server stopped before it was ready (exited with code 1)")

    def transcribe(self, clip, language):
        self.calls += 1
        return Transcript(self.text, 0.5, eou_s=0.05)

    def stop(self):
        self.stopped = True


class FakeSampler:
    def __init__(self):
        self.phase = "load"

    def start(self):
        pass

    def stop(self):
        return Samples(rss_mb=[100.0, 300.0], cpu_cores=[1.0, 3.0], vram_mb=[], gpu_util=[])


def _candidate(cid, *, missing=None, session=None, streaming=True):
    return cands.Candidate(cid, cid, cands.CPU, streaming, lambda: missing,
                           lambda scratch, avoid: session)


def _cells(table: str, cid: str) -> list[str]:
    line = next(line for line in table.splitlines() if line.startswith(f"| {cid} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def test_measures_installed_candidates_and_never_numbers_the_rest(clips):
    session = FakeSession()
    rows = run_all([_candidate("fake-ok", session=session), _candidate("fake-missing", missing="pkg not installed")],
                   clips, avoid=frozenset(), language="en", gpu=None,
                   sampler_factory=lambda pid: FakeSampler())
    assert [r.status for r in rows] == [OK, NOT_INSTALLED]
    assert session.calls == len(clips) + 1 and session.stopped  # one warm-up, then each clip
    ok = rows[0]
    assert ok.rtf == pytest.approx(1.0 / 4.0)        # 2 × 0.5 s over 4 s of audio
    assert ok.wer == 0.0 and ok.eou_s == pytest.approx(0.05)
    assert ok.cpu_cores == 2.0 and ok.peak_rss_mb == 300.0 and ok.vram_mb is None

    table = report.render(rows, clips, gpu_name=None)
    measured = _cells(table, "fake-ok")
    float(measured[3])  # the cold load is a number
    assert measured[4:9] == ["0.250", "0.050", "0.0%", "2.00", "300"]
    missing = _cells(table, "fake-missing")
    assert missing[2] == NOT_INSTALLED
    assert all(cell == report.NO_VALUE for cell in missing[3:11])
    assert missing[11] == "pkg not installed"


def test_a_failing_engine_gets_a_failed_row_and_is_stopped(clips):
    session = FakeSession(fail_on="ready")
    rows = run_all([_candidate("fake-broken", session=session)], clips, avoid=frozenset(),
                   language="en", gpu=None, sampler_factory=lambda pid: FakeSampler())
    assert rows[0].status == FAILED and "before it was ready" in rows[0].note
    assert session.stopped
    cells = _cells(report.render(rows, clips, None), "fake-broken")
    assert all(cell == report.NO_VALUE for cell in cells[3:11])


def test_an_install_check_that_raises_is_reported_not_fatal(clips):
    def boom():
        raise RuntimeError("catalog down")

    cand = cands.Candidate("fake-raise", "x", cands.CPU, False, boom, lambda s, a: FakeSession())
    rows = run_all([cand], clips, avoid=frozenset(), language="en", gpu=None)
    assert rows[0].status == NOT_INSTALLED and "catalog down" in rows[0].note


def test_word_errors_ignore_case_and_punctuation():
    assert word_errors("Hello, World!", "hello world") == (0, 2)
    assert word_errors("the cat sat", "the bat sat down") == (2, 3)
    assert word_errors("it’s fine", "it's fine") == (0, 2)


def test_clips_must_share_one_format(tmp_path):
    _wav(tmp_path / "loud.wav", 1.0, rate=44100, channels=2)
    with pytest.raises(ClipError, match="16 kHz mono"):
        load_clips(tmp_path)
    with pytest.raises(ClipError, match="clip folder not found"):
        load_clips(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ClipError, match="no .wav"):
        load_clips(tmp_path / "empty")


def test_whisper_candidate_names_what_is_missing(tmp_path):
    cand = next(c for c in cands.catalog(tmp_path) if c.id == "whisper-turbo-q5_0-cuda")
    assert "whisper-server not installed" in cand.missing()
    binary = cands._whisper_binary(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    assert "ggml-large-v3-turbo-q5_0.bin not downloaded" in cand.missing()
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / cands.WHISPER_WEIGHTS["q5_0"]).write_bytes(b"")
    assert cand.missing() is None


def test_whisper_cpu_candidate_disables_the_gpu_on_a_free_port(tmp_path):
    cpu = next(c for c in cands.catalog(tmp_path) if c.id == "whisper-turbo-f16-cpu")
    live = cands.registry_ports()
    session = cpu.open(tmp_path, live)
    cmd = session.child.cmd
    port = int(cmd[cmd.index("--port") + 1])
    assert "-ng" in cmd and port not in live
    assert "--max-context" in cmd  # the registry row's own decoding guards


def test_sherpa_cuda_refuses_the_cpu_only_build(tmp_path, monkeypatch):
    monkeypatch.setattr(cands.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cands.importlib.metadata, "version", lambda name: "2.0.3")
    by_id = {c.id: c for c in cands.catalog(tmp_path)}
    assert by_id["parakeet-unified-560ms-cuda"].missing() == "the installed sherpa-onnx is the CPU-only build"
    assert "not downloaded" in by_id["parakeet-unified-560ms-cpu"].missing()
    monkeypatch.setattr(cands.importlib.util, "find_spec", lambda name: None)
    assert "sherpa_onnx not installed" in by_id["parakeet-unified-240ms-cpu"].missing()


def test_foundry_candidate_runs_on_a_scratch_config_with_only_its_row(tmp_path, monkeypatch):
    shutil.copyfile(REPO / "config" / "models.yaml", tmp_path / "models.yaml")
    monkeypatch.setattr(host_profile, "CONFIG_PATH", tmp_path / "models.yaml")
    host_profile._CONFIG_CACHE.clear()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cand = next(c for c in cands.catalog(tmp_path) if c.id == "nemotron-asr")
    assert cand.streaming
    session = cand.open(scratch, cands.registry_ports())
    cmd = session.child.cmd
    written = yaml.safe_load(Path(cmd[cmd.index("--config") + 1]).read_text(encoding="utf-8"))
    assert written["hosts"]["local"]["enabled"] == ["nemotron_asr"]
    port = written["models"]["nemotron_asr"]["port"]
    assert port not in cands.registry_ports() and session.base.endswith(f":{port}")
    host_profile._CONFIG_CACHE.clear()


# A stand-in for whisper-server / the Foundry worker: /health, then the OpenAI audio shape.
_FAKE_SERVER = textwrap.dedent("""
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self._send(200 if self.path == "/health" else 404, {"status": "ok"})
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self._send(200, {"text": " fake words "})
        def log_message(self, *a):
            pass

    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")


def test_http_session_spawns_serves_and_stops_a_fake_server(tmp_path, clips):
    from src.audio.bench.sessions import free_port

    port = free_port()
    session = HttpServerSession([sys.executable, "-c", _FAKE_SERVER, str(port)], port=port,
                                model_name="fake", env=cands._python_env(), cwd=tmp_path,
                                log_path=tmp_path / "server.log", ready_timeout_s=30)
    try:
        session.start()
        session.wait_ready()
        result = session.transcribe(clips[0].path, "en")
        assert result.text == "fake words" and result.seconds >= 0
    finally:
        session.stop()
    assert session.child.exited() is not None


def test_http_session_reports_a_server_that_dies(tmp_path):
    session = HttpServerSession([sys.executable, "-c", "import sys; print('no model'); sys.exit(4)"],
                                port=1, model_name="x", env=cands._python_env(), cwd=tmp_path,
                                log_path=tmp_path / "dead.log", ready_timeout_s=30)
    session.start()
    with pytest.raises(EngineError, match="exited with code 4.*no model"):
        session.wait_ready()
    session.stop()


_FAKE_RUNNER = textwrap.dedent("""
    import sys
    from src.audio.bench import runner

    class Engine:
        def transcribe(self, path, language):
            if path.name == "b.wav":
                raise RuntimeError("decoder blew up")
            return {"text": "runner words", "seconds": 0.2, "eou_s": 0.01}

    def write(text):
        sys.stdout.write(text + "\\n")
        sys.stdout.flush()

    runner.serve(Engine(), sys.stdin, write)
""")


def test_runner_session_talks_json_lines_to_a_fake_engine(tmp_path, clips):
    session = RunnerSession([sys.executable, "-c", _FAKE_RUNNER], env=cands._python_env(),
                            cwd=REPO, log_path=tmp_path / "runner.log", ready_timeout_s=30)
    try:
        session.start()
        session.wait_ready()
        assert session.transcribe(clips[0].path, "en") == Transcript("runner words", 0.2, 0.01)
        with pytest.raises(EngineError, match="decoder blew up"):
            session.transcribe(clips[1].path, "en")
    finally:
        session.stop()


def test_runner_reads_pcm_as_unit_floats(tmp_path):
    path = tmp_path / "one.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes((16384).to_bytes(2, "little", signed=True) + (-32768).to_bytes(2, "little", signed=True))
    assert runner.read_samples(path) == [0.5, -1.0]


def test_fetch_refuses_unknown_items_and_skips_what_is_there(tmp_path, monkeypatch):
    from src.audio.bench import fetch

    assert fetch.fetch(tmp_path, ["nope"]) == 2
    (tmp_path / fetch.items()["sherpa-560ms"].target).mkdir(parents=True)

    def no_network(*a, **k):
        raise AssertionError("an item already there must not be downloaded")

    monkeypatch.setattr(fetch, "_download", no_network)
    assert fetch.fetch(tmp_path, ["sherpa-560ms"]) == 0
