"""Owner alert for repeated wrong passwords on the admin login.

The vendored :class:`~src.login_lockout.LoginLockout` calls ``on_alert`` inside
the login request, so the Telegram send runs on a daemon thread and the login
never waits on the network. The message names the app, the client address and
the failure count, never a password or a token.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from src.login_lockout import LockoutAlert
from src.notify import Notifier, NotifierError, TelegramConfig, build_notifier
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

APP_NAME = "Local LLM Hub Lite"


def _start_thread(send: Callable[[], None]) -> None:
    threading.Thread(target=send, daemon=True, name="login-alert").start()


def alert_text(alert: LockoutAlert) -> str:
    held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
    return f"🚨 {APP_NAME}: {alert.failures} wrong passwords in a row from {alert.key}{held}"


def make_alert_owner(
    get_config: Callable[[], WebappConfig],
    *,
    notifier_for: Callable[[WebappConfig], Notifier | None] | None = None,
    run: Callable[[Callable[[], None]], None] = _start_thread,
) -> Callable[[LockoutAlert], None]:
    """Build the ``on_alert`` callback; the config is read when an alert fires."""
    resolve = notifier_for or _telegram_notifier

    def alert_owner(alert: LockoutAlert) -> None:
        notifier = resolve(get_config())
        if notifier is None:
            logger.info("ℹ️ login alert not sent: no Telegram credentials configured")
            return
        text = alert_text(alert)

        def send() -> None:
            try:
                notifier.send_text(text)
            except NotifierError as exc:
                logger.warning(f"⚠️ login alert not delivered: {type(exc).__name__}")

        run(send)

    return alert_owner


def _telegram_notifier(cfg: WebappConfig) -> Notifier | None:
    has_both = bool(cfg.telegram_bot_token and cfg.telegram_alert_chat_id)
    return build_notifier(
        "telegram" if has_both else "none",
        TelegramConfig(cfg.telegram_bot_token, cfg.telegram_alert_chat_id),
    )
