"""Owner alert for repeated wrong passwords on the admin login.

Binds the vendored :mod:`src.login_alert` to this app's Telegram settings: the
config is read when an alert fires, so an edited chat id applies without a
restart. The send itself, its text and its logged outcome are the vendored
module's.
"""

from __future__ import annotations

from collections.abc import Callable

from src.login_alert import build_login_alert
from src.login_lockout import LockoutAlert
from src.notify import Notifier, TelegramConfig, build_notifier
from src.webapp_config import WebappConfig

APP_NAME = "Local LLM Hub Lite"


def make_alert_owner(
    get_config: Callable[[], WebappConfig],
    *,
    notifier_for: Callable[[WebappConfig], Notifier | None] | None = None,
    run: Callable[[Callable[[], None]], None] | None = None,
) -> Callable[[LockoutAlert], None]:
    """Build the ``on_alert`` callback; ``run`` replaces the send thread in tests."""
    resolve = notifier_for or _telegram_notifier

    def notifier() -> Notifier | None:
        return resolve(get_config())

    if run is None:
        return build_login_alert(APP_NAME, notifier)
    return build_login_alert(APP_NAME, notifier, run=run)


def _telegram_notifier(cfg: WebappConfig) -> Notifier | None:
    has_both = bool(cfg.telegram_bot_token and cfg.telegram_alert_chat_id)
    return build_notifier(
        "telegram" if has_both else "none",
        TelegramConfig(cfg.telegram_bot_token, cfg.telegram_alert_chat_id),
    )
