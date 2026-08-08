"""Chat-shape translation: request/response schemas and the Anthropic→OpenAI
bridge shared by the ``/v1/messages`` route in ``server.py``.

Holds the Pydantic schemas for the Anthropic ``/v1/messages`` shape plus
``_run_openai_backend`` — the dispatcher that translates an Anthropic-shape
request into an OpenAI-shape call against a local llama-server backend and
shapes the response back.

A leaf module with no dependency on ``server.py``'s ``app`` — mirrors
``server_common.py``'s reason for existing, so route modules (and this one)
can import each other without a circular import back into ``server.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel

from .model_registry import Model
from .openai_upstream import (
    UpstreamError,
    anthropic_to_openai_messages,
    call_openai_chat,
    openai_to_anthropic_envelope,
)


# ---- shared content-block helpers (unchanged shape) ----

class ContentBlock(BaseModel):
    type: str
    text: Optional[str] = None
    # Anthropic image block: {"type": "image", "source": {"type": "base64",
    # "media_type": "image/png", "data": "<b64>"}} or {"type": "url",
    # "url": "https://..."}. Kept loose so the 400 below can name the block.
    source: Optional[Dict[str, Any]] = None


class Message(BaseModel):
    role: str
    content: Union[str, List[ContentBlock]]


class MessagesRequest(BaseModel):
    model: str
    messages: List[Message]
    max_tokens: Optional[int] = None
    system: Optional[Union[str, List[ContentBlock]]] = None
    stream: bool = False
    temperature: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


def _content_to_text(content: Union[str, List[ContentBlock]]) -> str:
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for block in content:
        if block.type == "text" and block.text:
            parts.append(block.text)
    return "\n".join(parts)


def _system_to_text(system: Optional[Union[str, List[ContentBlock]]]) -> Optional[str]:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    return _content_to_text(system) or None


# ---- routing ----

def _run_openai_backend(model: Model, req: MessagesRequest) -> Dict[str, Any]:
    # On-demand lifecycle (#422): a cold ``startup: on_demand`` local backend
    # is spawned here and the request blocks until it answers (503 on load
    # failure) — same hook the OpenAI-shape route applies in server.py.
    from .server_common import ensure_backend_ready_or_503
    ensure_backend_ready_or_503(model)
    base_url = model.url
    if not base_url:
        raise HTTPException(status_code=500, detail=f"model {model.id} has no url")
    if any(
        isinstance(m.content, list)
        and any(b.type in ("image", "document") for b in m.content)
        for m in req.messages
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"backend {model.id!r} ({model.display_name}) is text-only. "
                "This hub serves local llama-server models only — send "
                "image/document requests elsewhere."
            ),
        )
    messages = anthropic_to_openai_messages(
        [m.model_dump() for m in req.messages],
        _system_to_text(req.system),
    )
    from . import on_demand as _on_demand
    try:
        with _on_demand.tracking(model):
            raw = call_openai_chat(
                base_url,
                model=model.display_name,
                messages=messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
            )
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return openai_to_anthropic_envelope(raw)
