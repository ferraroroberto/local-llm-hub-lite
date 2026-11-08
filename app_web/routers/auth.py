"""Password login → bearer token swap.

The bearer token in ``webapp_config.json`` is never sent to the client
until a correct password is presented. Failed attempts and successful
logins both land in ``webapp/auth.log`` (separate from the main hub log
so a phone-side review is easy).
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_middleware import is_pc_itself
from src.login_lockout import LoginLockout, retry_after_header
from src.runtime_files import checked_write_path
from src.webapp_config import WebappConfig

from ._helpers import PROJECT_ROOT, client_ip, maybe_json

logger = logging.getLogger(__name__)
auth_logger = logging.getLogger("llmhub.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def ensure_log_handler() -> None:
    """Attach the auth.log file handler exactly once. Idempotent."""
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _AUTH_LOG_PATH.resolve()
        for h in auth_logger.handlers
    ):
        return
    try:
        log_path = checked_write_path(_AUTH_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        auth_logger.addHandler(fh)
        auth_logger.setLevel(logging.INFO)
    except OSError as exc:
        logger.warning(f"⚠️  Could not open {_AUTH_LOG_PATH}: {exc}")


router = APIRouter()


@router.post("/api/login")
async def login(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    who = client_ip(request)
    lockout: LoginLockout = request.app.state.login_lockout
    # The PC itself is never locked out; the same check lets it skip the token.
    exempt = is_pc_itself(
        request.client.host if request.client else "", request.headers, cfg
    )
    wait = 0.0 if exempt else lockout.retry_after(who)
    if wait > 0:
        auth_logger.warning(f"🔒 Login refused for {who}: too many attempts")
        raise HTTPException(
            status_code=429,
            detail="too many attempts",
            headers=retry_after_header(wait),
        )
    if not cfg.auth_password:
        auth_logger.info(
            f"⚠️  Login attempt from {who} but no auth_password configured"
        )
        raise HTTPException(
            status_code=503, detail="password auth not configured"
        )
    if not cfg.auth_token:
        auth_logger.info(
            f"⚠️  Login attempt from {who} but no auth_token configured"
        )
        raise HTTPException(
            status_code=503, detail="bearer token not configured"
        )
    body = await maybe_json(request)
    presented = str(body.get("password") or "")
    if not presented or not hmac.compare_digest(presented, cfg.auth_password):
        auth_logger.warning(
            f"🚨 Failed password attempt from {who} "
            f"(presented: {len(presented)} chars)"
        )
        if not exempt:
            lockout.record_failure(who)
        raise HTTPException(status_code=401, detail="bad password")
    lockout.reset(who)
    auth_logger.info(f"🔓 Password login from {who}")
    return {"token": cfg.auth_token}
