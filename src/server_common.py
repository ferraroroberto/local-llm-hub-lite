"""Helpers shared by the hub's route modules.

``server.py`` (chat routes + app) and ``server_audio_asr.py`` (the
``/v1/audio/transcriptions`` proxy) need the same small set of
model-resolution helpers. They live here, in a leaf module with no
dependency on the FastAPI ``app``, so the route modules can import them
without a circular import back into ``server.py``.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from .model_registry import Model, enabled_models, resolve as resolve_model

logger = logging.getLogger(__name__)


def resolve_model_or_400(model_name: str) -> Model:
    """Resolve a request's ``model`` against the registry, or 400 with the
    list of names enabled on this host."""
    m = resolve_model(model_name)
    if m is None:
        known = [m.display_name for m in enabled_models()]
        raise HTTPException(
            status_code=400,
            detail=f"unknown model {model_name!r}. available on this host: {known}",
        )
    return m


def ensure_backend_ready_or_503(model: Model) -> None:
    """Bring a ``startup: on_demand`` local backend up before dispatch (#422).

    No-op for eager rows (``src.on_demand`` decides). A cold on-demand
    backend is spawned here and the call blocks until it answers — so the
    first request pays the load. A spawn/readiness failure surfaces as a
    distinct 503 rather than the generic connect-error 502, so "model failed
    to load" is tellable apart from "model crashed mid-serve" in client logs.
    """
    from . import on_demand

    try:
        on_demand.ensure_ready(model)
    except on_demand.OnDemandNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc))
