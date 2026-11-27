"""This app's wiring of the vendored installer (`setup.bat` -> `src/installer.py`).

The installer's own behaviour is the scaffold's to test; these tests drive it with this repo's real
`.fleet.toml` `[install]` table and config sample in a throwaway copy, with a fake runner in place of
`subprocess.run` (no venv is created, nothing is installed), and pin the playbook to the files the
app actually reads.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from src import installer, installer_migrate, password
from src.host_profile import hub_port

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXED_NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)
SAMPLE = "config/webapp_config.sample.json"
TARGET = "config/webapp_config.json"


class FakeRunner:
    """Stands in for `subprocess.run`: plays the venv appearing and answers every probe with exit 0."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if command[1:3] == ["-m", "venv"]:
            python = installer.venv_python(self.root)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)


def _clone(tmp_path: Path) -> Path:
    """A fresh checkout's tracked inputs: the real manifest, requirements, sample and registry."""
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    for name in (".fleet.toml", "requirements.txt", SAMPLE, "config/models.yaml"):
        shutil.copyfile(REPO_ROOT / name, root / name)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(root: Path, what_if: bool = False) -> tuple[installer.Report, FakeRunner]:
    runner = FakeRunner(root)
    return installer.run(root, runner=runner, now=lambda: FIXED_NOW, what_if=what_if), runner


def test_manifest_declares_this_apps_config_pair_and_settings() -> None:
    plan = installer.load_plan(REPO_ROOT)
    assert installer.config_pairs(REPO_ROOT, plan) == [(SAMPLE, TARGET)]
    assert TARGET in plan.settings


def test_fresh_clone_creates_the_config_and_second_run_is_unchanged(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    report, _ = _run(root)
    assert not report.failed
    assert (root / TARGET).read_bytes() == (root / SAMPLE).read_bytes()
    before = _snapshot(root)
    again, runner = _run(root)
    assert again.changes == []
    python = str(installer.venv_python(root))
    assert [call[:3] for call in runner.calls] == [  # the checklist probes only
        [python, "-m", "src.password"], [python, "-m", "src.install"], [python, "-m", "src.foundry"]]
    assert _snapshot(root) == before


def test_what_if_on_a_fresh_clone_writes_nothing(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    before = _snapshot(root)
    report, runner = _run(root, what_if=True)
    assert runner.calls == []
    assert _snapshot(root) == before
    assert installer.render(report).startswith("What-if: nothing below is changed.")


def test_existing_config_keeps_values_and_gains_only_missing_keys_after_a_backup(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    _run(root)
    target = root / TARGET
    older = {"auth_token": "kept-token", "extra_allowlist": ["10.1.2.3"]}
    target.write_text(json.dumps(older), encoding="utf-8")
    sample_keys = set(json.loads((root / SAMPLE).read_text(encoding="utf-8")))

    report, _ = _run(root)

    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["auth_token"] == "kept-token"
    assert updated["extra_allowlist"] == ["10.1.2.3"]
    assert set(updated) == sample_keys | set(older)
    backup = root / "config" / "webapp_config.json.bak-20260102-030405"
    assert json.loads(backup.read_text(encoding="utf-8")) == older
    added = [line for line in report.lines if line[0] == "added"]
    assert len(added) == 1
    assert "kept-token" not in added[0][2]


def test_the_model_registry_is_left_as_the_owner_edited_it(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    registry = root / "config" / "models.yaml"
    registry.write_text("hub: {port: 9000}\n", encoding="utf-8")
    _run(root)
    assert registry.read_text(encoding="utf-8") == "hub: {port: 9000}\n"
    assert not list((root / "config").glob("models.yaml*bak*"))


def _password_state(root: Path) -> str:
    """The sign-in password item as this app declares it, probed by a real `src.password` process."""

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if "src.password" in command:
            return subprocess.run([sys.executable, *command[1:]], **kwargs)
        return subprocess.CompletedProcess(command, 0)

    report = installer.Report(what_if=False)
    installer.build_checklist(root, installer.load_plan(root), report, runner, {})
    return next(state for state, item, _ in report.optional if "sign-in password" in item)


@pytest.fixture
def password_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A set-up checkout (venv, config, the password module) whose password file is a temp path."""
    root = _clone(tmp_path)
    shutil.copyfile(REPO_ROOT / SAMPLE, root / TARGET)
    (root / "src").mkdir()
    shutil.copyfile(REPO_ROOT / "src" / "password.py", root / "src" / "password.py")
    interpreter = installer.venv_python(root)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setenv(password.ENV_VAR, str(root / "config" / "app_password.json"))
    return root


@pytest.mark.parametrize(
    "content",
    [None, b"", b"{not json", b'{"password_hash": ""}', b'{"password_hash": "plain"}'],
    ids=["no-file", "empty", "corrupt", "no-hash", "not-a-hash"],
)
def test_password_item_is_off_without_a_usable_hash(password_clone: Path, content: bytes | None) -> None:
    if content is not None:
        (password_clone / "config" / "app_password.json").write_bytes(content)
    assert _password_state(password_clone) == "off"


def test_password_item_is_unknown_when_the_file_cannot_be_read(password_clone: Path) -> None:
    (password_clone / "config" / "app_password.json").mkdir()  # reading a directory fails on every OS
    assert _password_state(password_clone) == "unknown"


def test_password_item_is_available_with_a_stored_hash(password_clone: Path) -> None:
    password.set_password("4321", path=password_clone / "config" / "app_password.json")
    assert _password_state(password_clone) == "available"


def test_password_item_counts_the_old_password_the_sign_in_still_accepts(password_clone: Path) -> None:
    (password_clone / TARGET).write_text(json.dumps({"auth_password": "4321"}), encoding="utf-8")
    assert _password_state(password_clone) == "available"


def test_migration_declaration_carries_this_apps_local_files_and_nothing_tracked() -> None:
    migration = installer_migrate.parse_migration(installer_migrate.read_fleet_toml(REPO_ROOT))
    assert migration.endpoint is not None
    assert (migration.endpoint.port, migration.endpoint.version_path) == (hub_port(), "/admin/api/version")
    assert set(migration.secrets) <= set(migration.copy)
    assert migration.shared == ["local-llm-hub-lite"]
    # An ignored file leaves the fresh clone clean; a tracked or unignored one would not.
    for item in migration.copy:
        path = item if (REPO_ROOT / item).suffix else f"{item}/probe"
        ignored = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=REPO_ROOT, timeout=60,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert ignored.returncode == 0, item


@pytest.mark.windows_only
@pytest.mark.parametrize(
    "args, exit_code, banner",
    [([], 0, True), (["-WhatIf"], 0, True), (["-MigrateFrom", "old", "-Json"], 0, False),
     (["--migrate-from=old"], 0, False), ([], 3, False)],
    ids=["plain", "what-if", "migrate-json", "migrate-long", "failed"],
)
def test_setup_bat_adds_its_banner_to_a_plain_run_only(
    tmp_path: Path, args: list[str], exit_code: int, banner: bool
) -> None:
    shutil.copyfile(REPO_ROOT / "setup.bat", tmp_path / "setup.bat")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "installer.py").write_text(f"raise SystemExit({exit_code})\n", encoding="utf-8")
    done = subprocess.run(["cmd.exe", "/d", "/c", str(tmp_path / "setup.bat"), *args], cwd=tmp_path, timeout=60,
                          capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == exit_code
    assert ("Next steps" in done.stdout) is banner


def test_playbook_names_what_the_migration_copies_and_the_readme_links_it() -> None:
    playbook = (REPO_ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    migrate = playbook.split("\n## Migrate\n")[1]
    declared = installer_migrate.parse_migration(installer_migrate.read_fleet_toml(REPO_ROOT)).copy
    for carried in (*declared, "config/models.yaml", "usage.sqlite3"):
        assert carried in migrate
    assert "docs/install.md" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")
