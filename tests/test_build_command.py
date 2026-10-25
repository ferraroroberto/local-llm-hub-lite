"""``build_command`` launch arguments for the two backend engines.

The hub reaches every backend over 127.0.0.1 (``Model.url``), so the
command line it builds must use that same listen address. A row's own
``args`` are appended last, so an operator can still override it there.
"""

from __future__ import annotations

from src.process import backend_process as bp
from src.model_registry import Model


def _host_value(cmd: list[str]) -> str:
    """The value of the last ``--host`` flag, as the backend parses it."""
    values = [cmd[i + 1] for i, part in enumerate(cmd[:-1]) if part == "--host"]
    assert values, f"no --host in {cmd}"
    return values[-1]


def _patch_files(monkeypatch, tmp_path) -> None:
    """Point the binary lookups and the weights root at real temp files so
    ``build_command`` gets past its existence checks."""
    llama = tmp_path / "llama-server"
    whisper = tmp_path / "whisper-server"
    llama.write_text("", encoding="utf-8")
    whisper.write_text("", encoding="utf-8")
    (tmp_path / "weights.bin").write_text("", encoding="utf-8")
    monkeypatch.setattr(bp, "llama_server_binary", lambda: llama)
    monkeypatch.setattr(bp, "whisper_server_binary", lambda: whisper)
    monkeypatch.setattr(bp, "PROJECT_ROOT", tmp_path)


def _model(**kw) -> Model:
    base = dict(id="m", display_name="m", port=8099, model_path="weights.bin")
    base.update(kw)
    return Model(**base)


def test_build_command_listen_address_llama(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    cmd = bp.build_command(_model(backend="openai", engine="llama-server"))
    assert _host_value(cmd) == "127.0.0.1"


def test_build_command_listen_address_whisper(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    cmd = bp.build_command(_model(backend="whisper", engine="whisper-server"))
    assert _host_value(cmd) == "127.0.0.1"


def test_build_command_row_args_override_listen_address(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    cmd = bp.build_command(
        _model(backend="openai", engine="llama-server", args=["--host", "192.0.2.10"])
    )
    assert _host_value(cmd) == "192.0.2.10"
