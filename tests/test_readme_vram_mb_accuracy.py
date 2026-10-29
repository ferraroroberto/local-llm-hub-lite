"""Structural check for the recurring documentation-audit bucket (issue #94).

README.md and config/models.yaml's field-reference comments called
``est_vram_mb`` "display only" while src/on_demand.py actually feeds it into
the on-demand overcommit warning, checked against the host's ``vram_mb``
ceiling -- a field config/models.yaml's own field reference never
documented and the README's config block never showed. Ground-truth both
claims against on_demand.py/host_profile.py so a future edit that
reintroduces "display only" or drops the hosts.vram_mb mention fails here
instead of surfacing as a third consecutive audit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
MODELS_YAML = (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
ON_DEMAND = (ROOT / "src" / "on_demand.py").read_text(encoding="utf-8")


def _readme_config_reference_section() -> str:
    match = re.search(r"## Configuration reference\n(.+?)\n## ", README, re.S)
    assert match, "README's '## Configuration reference' section moved or was removed — update this check's anchor"
    return match.group(1)


def test_on_demand_actually_checks_est_vram_mb_against_host_vram_mb():
    """Ground truth: on_demand.py really does sum running models'
    ``est_vram_mb`` and compare it to the host's ``vram_mb`` ceiling. If
    this ever stops being true, the "not display-only" claims asserted
    below would themselves go stale — guard the guard."""
    assert "host.vram_mb" in ON_DEMAND
    assert "est_vram_mb" in ON_DEMAND


def test_models_yaml_field_reference_does_not_call_est_vram_mb_display_only():
    assert "display only" not in MODELS_YAML.lower(), (
        "config/models.yaml's field reference still calls est_vram_mb "
        "'display only', but src/on_demand.py feeds it into the on-demand "
        "overcommit warning"
    )
    assert "est_vram_mb" in MODELS_YAML and "overcommit" in MODELS_YAML.lower()


def test_models_yaml_documents_hosts_vram_mb_field():
    assert re.search(r"#\s*vram_mb\b", MODELS_YAML), (
        "config/models.yaml's field reference comment doesn't document the "
        "hosts.<id>.vram_mb overcommit ceiling (src/host_profile.py) — a "
        "user has no way to discover it short of reading the source"
    )


def test_readme_config_reference_does_not_call_est_vram_mb_display_only():
    section = _readme_config_reference_section()
    assert "display only" not in section.lower(), (
        "README's Configuration reference still calls est_vram_mb "
        "'display only', but it feeds the on-demand overcommit warning"
    )
    assert "overcommit" in section.lower()


def test_readme_config_reference_documents_hosts_vram_mb_field():
    section = _readme_config_reference_section()
    assert "vram_mb" in section and "hosts:" in section, (
        "README's Configuration reference doesn't show hosts.<id>.vram_mb "
        "in the models.yaml structure block"
    )
