"""Put a bearer token where the hub and /admin both read it.

The token source is ``config/webapp_config.json`` (``current_auth_token``), which
``tests/conftest.py`` redirects to a throwaway file for every test; a test that
needs a token written there calls ``pin_auth_token``.
"""

from __future__ import annotations

from src import webapp_config
from src.webapp_config import WebappConfig, save_webapp_config


def pin_auth_token(token: str) -> None:
    save_webapp_config(WebappConfig(auth_token=token))
    webapp_config.reset_auth_token_cache()
