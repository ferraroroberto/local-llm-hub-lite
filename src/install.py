"""First-run and health checks shared by the CLI and the admin SPA.

Each check inspects state and returns a `Check` row with a status of
`ok` | `missing` | `warn` | `error`. Fix functions are separate and do
the actual installing/downloading when the user opts in.

Usage:
    python -m src.install             # print a table, exit 1 if any error/missing
    python -m src.install --fix       # run fix_fn for every fixable row
    python -m src.install --json      # machine-readable output
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

from .backend_process import llama_server_binary, whisper_server_binary
from .host_profile import hub_port, resolve as resolve_host
from .model_registry import local_models
from .no_window import NO_WINDOW

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATUS_ORDER = {"ok": 0, "warn": 1, "missing": 2, "error": 3}


@dataclass
class Check:
    id: str
    label: str
    status: str = "ok"        # ok | warn | missing | error
    detail: str = ""
    fix_id: Optional[str] = None
    fix_label: Optional[str] = None


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    @property
    def worst_status(self) -> str:
        return max((c.status for c in self.checks), key=lambda s: STATUS_ORDER.get(s, 0), default="ok")

    @property
    def ok(self) -> bool:
        return self.worst_status in ("ok", "warn")


# ---------- individual checks ----------

def _check_python_venv() -> Check:
    ver = sys.version_info
    if ver < (3, 10):
        return Check("python", "Python >= 3.10", "error",
                     f"found {ver.major}.{ver.minor}.{ver.micro}")
    try:
        in_project_venv = Path(sys.prefix).resolve().is_relative_to((PROJECT_ROOT / ".venv").resolve())
    except (AttributeError, ValueError):
        in_project_venv = str(Path(sys.prefix).resolve()).startswith(str((PROJECT_ROOT / ".venv").resolve()))
    if not in_project_venv:
        return Check("python", "Python >= 3.10, running from .venv", "warn",
                     f"python={sys.version.split()[0]} prefix={sys.prefix} (not the project .venv)")
    return Check("python", "Python >= 3.10, running from .venv", "ok",
                 f"python={sys.version.split()[0]} prefix={sys.prefix}")


_REQUIRED_DEPS = [
    "fastapi", "uvicorn", "httpx",
    "yaml", "huggingface_hub", "pydantic", "python_multipart",
]


def _check_deps() -> Check:
    missing = []
    for mod in _REQUIRED_DEPS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return Check("deps", "Python deps installed", "missing",
                     f"missing: {', '.join(missing)}",
                     fix_id="deps", fix_label="pip install -r requirements.txt")
    return Check("deps", "Python deps installed", "ok",
                 f"{len(_REQUIRED_DEPS)} packages OK")


def _check_host_profile() -> Check:
    try:
        h = resolve_host()
    except Exception as e:
        return Check("host", "Host profile resolves", "error", str(e))
    return Check("host", "Host profile resolves", "ok",
                 f"host={h.id} ({h.source}); enabled local models: {h.enabled or '(none)'}")


def _probe_cli_version(
    *,
    check_id: str,
    label: str,
    exe: Optional[str],
    version_args: List[str],
    not_found_detail: str,
    not_found_status: str = "warn",
    fix_id: Optional[str] = None,
    fix_label: Optional[str] = None,
    ok_codes: tuple = (0,),
) -> Check:
    """Shared shape for the "is this executable here and runnable?" checks.

    The caller resolves ``exe`` to a runnable path (``shutil.which(...)`` for
    PATH tools, or ``str(bin_path)`` when the vendored binary exists, else
    ``None``). We then run ``exe version_args`` with a short timeout and
    classify the exit code:

      * ``exe is None``      → ``not_found_status`` (warn for optional CLIs,
                               missing + fix for installable binaries).
      * returncode in ``ok_codes`` → ``ok``, detail = first output line.
      * any other returncode → ``warn`` ("<args> exited N").
      * subprocess raised    → ``warn`` (the exception text).
    """
    if not exe:
        return Check(check_id, label, not_found_status, not_found_detail,
                     fix_id=fix_id, fix_label=fix_label)
    try:
        r = subprocess.run([str(exe), *version_args],
                           capture_output=True, text=True, timeout=10,
                           creationflags=NO_WINDOW)
        if r.returncode in ok_codes:
            first = ((r.stdout or r.stderr).strip().splitlines() or ["ok"])[0]
            return Check(check_id, label, "ok", first)
        return Check(check_id, label, "warn",
                     f"{' '.join(version_args)} exited {r.returncode}")
    except Exception as e:  # noqa: BLE001
        return Check(check_id, label, "warn", str(e))


def _nvidia_smi_gpu_check() -> Check:
    """nvidia-smi name+VRAM probe — shared by the Windows and Linux branches
    (the tower and the CUDA satellites both read their GPU the same way)."""
    nv = shutil.which("nvidia-smi")
    if not nv:
        return Check("gpu", "GPU / accelerator detected", "warn", "nvidia-smi not found")
    try:
        r = subprocess.run(
            [nv, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Check("gpu", "GPU / accelerator detected", "ok", r.stdout.strip().splitlines()[0])
        return Check("gpu", "GPU / accelerator detected", "warn",
                     f"nvidia-smi exit {r.returncode}: {r.stderr[:200]}")
    except Exception as e:
        return Check("gpu", "GPU / accelerator detected", "warn", str(e))


def _check_gpu() -> Check:
    if sys.platform == "win32":
        return _nvidia_smi_gpu_check()
    if sys.platform == "darwin":
        mach = platform.machine()
        if mach == "arm64":
            return Check("gpu", "Apple Silicon GPU (Metal)", "ok", f"arch={mach}")
        return Check("gpu", "GPU / accelerator detected", "warn",
                     f"darwin but arch={mach} — MLX / Metal expect arm64")
    if sys.platform.startswith("linux"):
        # The CUDA satellites (gaming, later openclaw) read their NVIDIA GPU
        # exactly like the tower does.
        return _nvidia_smi_gpu_check()
    return Check("gpu", "GPU / accelerator detected", "warn",
                 f"unknown platform {sys.platform}")


def _check_llama_cpp() -> Check:
    bin_path = llama_server_binary()
    return _probe_cli_version(
        check_id="llama_cpp",
        label="llama.cpp binary installed",
        exe=str(bin_path) if bin_path.exists() else None,
        version_args=["--version"],
        not_found_detail=f"expected at {bin_path}",
        not_found_status="missing",
        fix_id="llama_cpp",
        fix_label="scripts/install_llama_cpp.py (downloads the platform-matching release)",
    )


def _check_models() -> List[Check]:
    rows: List[Check] = []
    for m in local_models():
        if m.backend not in ("openai", "whisper") or not m.model_path:
            continue
        path = (PROJECT_ROOT / m.model_path).resolve()
        label = f"Model present: {m.display_name}"
        if path.exists() and path.is_file():
            size_gb = path.stat().st_size / (1024 ** 3)
            rows.append(Check(f"model_{m.id}", label, "ok", f"{path.name} ({size_gb:.1f} GB)"))
        else:
            rows.append(Check(
                f"model_{m.id}", label, "missing",
                f"expected at {path}",
                fix_id=f"download_{m.id}",
                fix_label=f"scripts/download_models.py --only {m.id}",
            ))
    return rows


def _whisper_enabled() -> bool:
    return any(m.engine == "whisper-server" for m in local_models())


def _check_whisper_cpp() -> Check:
    bin_path = whisper_server_binary()
    # whisper-server prints usage on --help and may exit non-zero (0 or 1).
    return _probe_cli_version(
        check_id="whisper_cpp",
        label="whisper.cpp binary installed",
        exe=str(bin_path) if bin_path.exists() else None,
        version_args=["--help"],
        not_found_detail=f"expected at {bin_path}",
        not_found_status="missing",
        fix_id="whisper_cpp",
        fix_label="scripts/install_whisper_cpp.py (downloads the platform-matching release)",
        ok_codes=(0, 1),
    )


def _port_in_use(port: int) -> bool:
    # Probe by connecting, not binding: on Windows a bind to 127.0.0.1
    # succeeds even while another process listens on the same port's
    # wildcard address (0.0.0.0), so a bind-probe reports a busy port as
    # free. A successful connect is proof something is listening.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _check_ports() -> List[Check]:
    rows: List[Check] = []
    for label, port in [("hub", hub_port())] + [
        (m.display_name, m.port)
        for m in local_models()
        if m.backend in ("openai", "whisper") and m.port and not m.virtual
    ]:
        if _port_in_use(port):
            rows.append(Check(
                f"port_{port}", f"Port {port} free ({label})", "warn",
                f"port {port} already in use — may be our own process"
            ))
        else:
            rows.append(Check(f"port_{port}", f"Port {port} free ({label})", "ok", f"port {port} free"))
    return rows


# ---------- report + fixes ----------

# Brief cache for run_all_checks(use_cache=True) (issue #198). The battery
# shells out to `nvidia-smi` / `llama-server --version` via blocking
# subprocess.run and "can pin the entire uvicorn worker for
# seconds" (see app_web/routers/hub.py's install_status docstring) — yet
# install_fix/install_fix_all only need it to *locate* one check by
# fix_id, and the admin UI always calls install_status (which populates
# this cache) moments before a user clicks a fix button. A short TTL means
# a fix click right after a status load reuses that report instead of
# re-running the whole battery; a stale/empty cache still falls back to a
# fresh run, so correctness never depends on the cache being warm.
_CACHE_TTL_S = 5.0
_cached_report: Optional[Report] = None
_cached_at: float = 0.0


def run_all_checks(*, use_cache: bool = False) -> Report:
    """Run every install check. Every call refreshes the brief cache used
    by ``use_cache=True`` callers; pass ``use_cache=True`` to reuse a
    report computed within the last ``_CACHE_TTL_S`` seconds instead of
    forcing a fresh (expensive) battery run.
    """
    global _cached_report, _cached_at
    if use_cache and _cached_report is not None and (time.monotonic() - _cached_at) < _CACHE_TTL_S:
        return _cached_report

    checks: List[Check] = [
        _check_python_venv(),
        _check_deps(),
        _check_host_profile(),
        _check_gpu(),
        _check_llama_cpp(),
    ]
    if _whisper_enabled():
        checks.append(_check_whisper_cpp())
    checks.extend(_check_models())
    checks.extend(_check_ports())
    report = Report(checks=checks)
    _cached_report = report
    _cached_at = time.monotonic()
    return report


FixFn = Callable[[], None]


def _fix_deps() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")],
        check=True,
        creationflags=NO_WINDOW,
    )


def _fix_llama_cpp() -> None:
    from scripts import install_llama_cpp  # type: ignore
    install_llama_cpp.main()


def _fix_whisper_cpp() -> None:
    from scripts import install_whisper_cpp  # type: ignore
    install_whisper_cpp.main()


def _fix_download(model_id: str) -> Callable[[], None]:
    def _fix() -> None:
        from scripts import download_models  # type: ignore
        download_models.download_one(model_id)
    return _fix


def fix_fn_for(check: Check) -> Optional[FixFn]:
    if check.fix_id == "deps":
        return _fix_deps
    if check.fix_id == "llama_cpp":
        return _fix_llama_cpp
    if check.fix_id == "whisper_cpp":
        return _fix_whisper_cpp
    if check.fix_id and check.fix_id.startswith("download_"):
        return _fix_download(check.fix_id[len("download_"):])
    return None


# ---------- CLI ----------

_STATUS_GLYPH = {"ok": "OK", "warn": "!!", "missing": "??", "error": "xx"}


def _print_report(report: Report) -> None:
    width = max(len(c.label) for c in report.checks) + 2
    for c in report.checks:
        glyph = _STATUS_GLYPH.get(c.status, "?")
        log.info("  %s [%7s] %s %s", glyph, c.status, c.label.ljust(width), c.detail)
    log.info("")
    if report.ok:
        log.info("overall: %s", report.worst_status)
    else:
        log.info("overall: %s - run with --fix to attempt repairs", report.worst_status)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m src.install", description="First-run checks for local-llm-hub")
    p.add_argument("--fix", action="store_true", help="attempt to fix every fixable row")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    report = run_all_checks()

    if args.fix:
        for c in list(report.checks):
            if c.status in ("missing", "error"):
                fn = fix_fn_for(c)
                if fn is None:
                    continue
                log.info("-> fixing %s: %s", c.id, c.fix_label)
                try:
                    fn()
                except Exception as e:
                    log.error("   fix failed: %s", e)
        report = run_all_checks()

    if args.json:
        print(json.dumps([c.__dict__ for c in report.checks], indent=2))
    else:
        _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
