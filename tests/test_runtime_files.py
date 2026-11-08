"""checked_write_path refuses code folders and passes everything else through."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from src import runtime_files
from src.runtime_files import CODE_FOLDERS, CodeFolderWriteRefused, checked_write_path

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", CODE_FOLDERS)
def test_refuses_a_path_under_each_code_folder(folder: str) -> None:
    with pytest.raises(CodeFolderWriteRefused):
        checked_write_path(REPO_ROOT / folder / "stray.log")


def test_refuses_a_code_folder_reached_through_dotdot() -> None:
    with pytest.raises(CodeFolderWriteRefused):
        checked_write_path(REPO_ROOT / "data" / ".." / "src" / "x.txt")


@pytest.mark.parametrize("folder", ["data", "config", "webapp", "vendor", "models"])
def test_allows_gitignored_runtime_folders_in_the_checkout(folder: str) -> None:
    path = REPO_ROOT / folder / "x.log"
    assert checked_write_path(path) == path


def test_allows_a_path_outside_the_checkout(tmp_path: Path) -> None:
    path = tmp_path / "out.json"
    assert checked_write_path(str(path)) == path


def test_code_folders_match_the_ratchet_config() -> None:
    config = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    table = config["runtime_write_ratchet"]
    assert tuple(table["code_folders"]) == runtime_files.CODE_FOLDERS
    assert "checked_write_path" in table["safe_calls"]
