"""Every observability field the ring carries has a producer in this fork.

The ring's fields were inherited from the full fork, where cloud backends and
routes filled some of them; here a field nothing assigns ships a constant to
the Hub tab. This fails when an ``ObservabilityCtx`` slot is never assigned
as ``ctx.<slot> = ...`` anywhere under ``src/``.
"""

import re
from pathlib import Path

from src.hub_observability import ObservabilityCtx

SRC = Path(__file__).resolve().parents[2] / "src"
# ``start_ns`` is stamped by the context's own constructor, not by a route.
SELF_STAMPED = {"start_ns"}


def test_every_ctx_slot_has_a_producer() -> None:
    sources = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    unproduced = [
        slot
        for slot in ObservabilityCtx.__slots__
        if slot not in SELF_STAMPED
        and not re.search(rf"\bctx\.{slot}\s*=(?!=)", sources)
    ]
    assert not unproduced, (
        f"ObservabilityCtx slots never assigned by any route: {unproduced} — "
        "wire a producer or drop the field from the record, context and serializer"
    )
