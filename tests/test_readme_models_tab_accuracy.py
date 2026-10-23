"""Structural check for the recurring documentation-audit bucket (issue #55).

The README's "### Models" paragraph drifted from the shipped SPA twice in a
row (2026-08-13, 2026-10-22): it listed a per-card ``engine`` field the tile
never renders, and promised "a log-tail drawer" the SPA never shipped.
Both claims are checked against their ground truth in ``models.js`` instead
of being hand-verified per audit pass.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
MODELS_JS = (ROOT / "app_web" / "static" / "models.js").read_text(encoding="utf-8")


def _models_tab_section() -> str:
    match = re.search(r"### Models\n\n(.+?)\n\n### Playground", README, re.S)
    assert match, "README's '### Models' section moved or was removed — update this check's anchor"
    return match.group(1)


def test_models_tab_does_not_claim_a_log_drawer():
    """models.js states explicitly there is no per-model log tailing — the
    README's Models-tab paragraph must not promise a log drawer the SPA
    never shipped."""
    # Strip the block comment's " * " line leaders before collapsing
    # whitespace, so a disclaimer wrapped across comment lines still reads
    # as one sentence.
    normalized_js = re.sub(r"\n\s*\*\s?", " ", MODELS_JS)
    normalized_js = re.sub(r"\s+", " ", normalized_js)
    assert "No per-model log tailing" in normalized_js, (
        "models.js dropped its no-per-model-log-tailing disclaimer — "
        "re-check whether a log drawer shipped and update the README to match"
    )
    section = _models_tab_section().lower()
    claims_a_drawer = re.search(r"\ba\s+log(?:-tail)?\s+drawer\b", section)
    assert not claims_a_drawer, (
        "README's Models-tab paragraph claims a log-tail drawer that "
        "models.js's own header comment says was never shipped"
    )


def test_models_tab_field_list_matches_rendered_meta_fields():
    """The per-card field list README quotes must name a field models.js's
    meta line actually renders (``backend``), not the unrendered registry
    field (``engine``) that caused the prior drift."""
    section = _models_tab_section()
    sentence = section.split(".", 1)[0]  # "One card per registry row: ..."
    assert "engine" not in sentence.lower(), (
        "README lists 'engine' as a per-card field, but models.js's meta "
        "line renders m.backend, never m.engine (app_web/static/models.js)"
    )
    assert "backend" in sentence.lower(), (
        "README's Models-tab field list should name the field models.js's "
        "meta line actually renders (m.backend)"
    )
