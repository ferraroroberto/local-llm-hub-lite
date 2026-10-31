"""Regression coverage for cross-platform hub process discovery."""

from __future__ import annotations

from types import SimpleNamespace

import psutil

from src.process import server_process


def test_find_port_pids_is_a_view_over_the_snapshot(monkeypatch):
    monkeypatch.setattr(
        server_process, "snapshot_listening_pids", lambda: {8098: [40368], 8081: [7]}
    )

    assert server_process.find_port_pids(8098) == [40368]
    assert server_process.find_port_pids(9999) == []


def test_find_port_pids_parses_posix_lsof_through_the_single_parser(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise psutil.AccessDenied()

    def fake_run(command, **_kwargs):
        assert command[0] == "lsof"
        return SimpleNamespace(stdout="p40368\nPTCP\nn*:8098\np7\nn127.0.0.1:8081\n")

    monkeypatch.setattr(psutil, "net_connections", refuse)
    monkeypatch.setattr(server_process.sys, "platform", "darwin")
    monkeypatch.setattr(server_process.subprocess, "run", fake_run)

    assert server_process.find_port_pids(8098) == [40368]
    assert server_process.find_port_pids(8081) == [7]
