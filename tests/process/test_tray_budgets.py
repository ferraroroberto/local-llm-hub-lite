"""The tray's waits are tied to the budgets they have to outlast."""

from __future__ import annotations

from src.process.backend_process import (
    STOP_GRACE_S,
    STOP_KILL_WAIT_S,
    STOP_TERMINATE_WAIT_S,
)
from src.process.serving_state import LOAD_DEADLINE_S
from tray import tray


def test_model_wait_outlasts_the_hub_load_budget():
    """The tray must not call a model unreachable while the hub still counts
    it as loading."""
    assert tray.MODEL_READY_WAIT_S > LOAD_DEADLINE_S


def test_admin_call_outlasts_a_full_backend_stop():
    """A model stop can run the whole CTRL_BREAK, terminate, kill sequence."""
    worst_stop_s = STOP_GRACE_S + STOP_TERMINATE_WAIT_S + STOP_KILL_WAIT_S
    assert tray.ADMIN_CALL_TIMEOUT_S > worst_stop_s
