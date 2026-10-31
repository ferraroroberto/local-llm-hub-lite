"""Local LLM Hub admin webapp — FastAPI sub-app mounted at /admin.

Tabs: Hub, Models, Playground. Lives inside the hub's own FastAPI
process; no second port, no second Python process.

Auth model (mirrors app-launcher):
  * Loopback callers (PC itself) bypass the bearer token.
  * Non-loopback callers must present ``Authorization: Bearer <token>``
    (or ``?token=…`` on the initial URL). Token is hashed-compare.
"""

from __future__ import annotations

from .server import create_app

__all__ = ["create_app"]
