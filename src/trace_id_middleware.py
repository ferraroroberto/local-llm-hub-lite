"""Response header contract — X-Trace-Id, request-id, and Anthropic parity.

Many small clients — voice-transcriber, ad-hoc curl scripts — mint a UUID4
and shove it in an ``X-Trace-Id`` header. This middleware bridges:

- **Inbound:** if a request carries ``X-Trace-Id``, derive a deterministic
  128-bit trace ID from the client's value (BLAKE2b UUID4 → 16 bytes). Two
  calls with the same X-Trace-Id yield the same derived trace ID.

- **Outbound:** every response gets ``X-Trace-Id`` set to that derived
  trace ID (hex) when the client sent one.

On top of that contract this middleware also speaks Anthropic's header
vocabulary (#461), because it is the one layer that sees every response —
including the 401s and 4xx envelopes raised before any route runs:

- **``request-id``** on every non-static response, aliased to the same
  trace ID ``X-Trace-Id`` carries, so one identifier ties the client's
  record and the hub log line together. When the client sent no
  ``X-Trace-Id`` a random 128-bit ID keeps the header unconditional
  rather than silently absent.
- **``anthropic-version`` / ``anthropic-beta``**, on the Anthropic-shape
  routes only, per :mod:`src.anthropic_headers`.

``X-Trace-Id`` itself is untouched — existing clients read it and must
keep seeing exactly what they saw before.

Sits OUTERMOST in the middleware stack.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid as _uuid
from typing import Any, Awaitable, Callable, Optional

from .anthropic_errors import is_anthropic_shape_path
from .anthropic_headers import parity_headers

logger = logging.getLogger(__name__)

# Routes that should NEVER carry a trace ID (static assets, admin SPA
# polling) — adding the header doesn't break anything but wastes bytes
# and clutters the response shape. We still process headers on every
# request because the cost is a dict lookup.
_NOISE_PREFIXES = ("/admin/static",)

# Paths whose request-id is worth a log line. The whole point of emitting
# the header is that a caller can quote it in a support question and the
# request be findable — so the API surface is logged and the admin SPA's
# own polling is not.
_LOGGED_PREFIXES = ("/v1/",)


def derive_trace_id_from_uuid(value: str) -> Optional[int]:
    """Map a caller-supplied UUID4 / hex string to a 128-bit trace ID.

    Accepts: 32-char hex (already a valid trace ID), or a UUID in any
    common form (hyphenated or not). Anything else → BLAKE2b of the raw
    string, so callers passing arbitrary opaque strings still correlate.
    Returns ``None`` for empty/zero input.

    Determinism matters: two requests with the same client-side trace
    ID must derive the same value, so we BLAKE2b the input into a stable
    16-byte digest. (Inlined from the retired ``src.observability`` —
    behavior preserved exactly.)
    """
    raw = (value or "").strip().lower()
    if not raw:
        return None

    # Plain 32-char hex — already a valid trace ID.
    if len(raw) == 32:
        try:
            int(raw, 16)
            return int(raw, 16) or None  # all-zero is invalid
        except ValueError:
            pass

    # Try UUID4 in either hyphenated or compact form.
    candidate: Optional[str] = None
    try:
        candidate = _uuid.UUID(raw).hex
    except (ValueError, AttributeError):
        candidate = None

    if candidate is None:
        # Last resort: BLAKE2b the original string into a 16-byte ID so
        # callers passing arbitrary opaque strings still get correlation.
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=16).digest()
        as_int = int.from_bytes(digest, "big")
        return as_int or None

    # Hash the UUID hex too so we don't collide with a real trace ID
    # that happens to share the same 32-char prefix.
    digest = hashlib.blake2b(candidate.encode("utf-8"), digest_size=16).digest()
    as_int = int.from_bytes(digest, "big")
    return as_int or None


def _fallback_request_id() -> str:
    """A trace-ID-shaped random ID, for when the client sent no X-Trace-Id."""
    return format(secrets.randbits(128), "032x")


class TraceIdHeaderMiddleware:
    """Pure-ASGI middleware — installed OUTERMOST.

    Pure-ASGI rather than ``BaseHTTPMiddleware`` because we need to
    read the request scope's headers list before the app runs.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        method = scope.get("method", "?")
        is_noise = any(path.startswith(p) for p in _NOISE_PREFIXES)
        request_headers = list(scope.get("headers") or [])
        tid_hex = "" if is_noise else self._derived_trace_id_hex(request_headers)

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                # Never clobber a header the handler set itself.
                present = {name.lower() for name, _ in headers}
                if tid_hex and b"x-trace-id" not in present:
                    headers.append((b"x-trace-id", tid_hex.encode("ascii")))
                if not is_noise and b"request-id" not in present:
                    request_id = tid_hex or _fallback_request_id()
                    headers.append((b"request-id", request_id.encode("ascii")))
                    if any(path.startswith(p) for p in _LOGGED_PREFIXES):
                        logger.info(
                            "ℹ️ request-id=%s %s %s -> %s",
                            request_id,
                            method,
                            path,
                            message.get("status", "?"),
                        )
                if not is_noise and is_anthropic_shape_path(path):
                    for name, value in parity_headers(request_headers):
                        if name not in present:
                            headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _derived_trace_id_hex(raw_headers: list) -> str:
        """Deterministic 32-hex trace ID derived from the client's
        ``X-Trace-Id`` header, or ``""`` when absent/unusable."""
        x_trace_id_value = b""
        for name, value in raw_headers:
            if name == b"x-trace-id":
                x_trace_id_value = value
                break
        if not x_trace_id_value:
            return ""
        try:
            derived = derive_trace_id_from_uuid(
                x_trace_id_value.decode("ascii", errors="replace")
            )
        except Exception:  # noqa: BLE001
            derived = None
        if derived is None:
            return ""
        return format(derived, "032x")
