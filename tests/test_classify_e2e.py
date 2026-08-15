"""Unit tests for the diff-proportionate e2e classifier (project-scaffolding#180).

Two jobs:

1. Prove the *mechanism* — first-match-wins rule ordering, the STATIC/FULL/NONE
   tier maths across a mixed diff, and above all the **fail-safe**: an empty
   diff, an unmatched path, a missing/empty/invalid `[e2e]` table all route to
   the full suite. Uncertainty must never narrow coverage.
2. Act as the **anti-drift guard** for this repo's own declaration — load the
   real `.fleet.toml` `[e2e]` block and assert representative paths land in the
   tier their rule intends, so a later edit that silently under-routes a real
   surface fails here.
"""

from __future__ import annotations

from pathlib import Path

from scripts.classify_e2e import (
    Category,
    E2EConfig,
    Rule,
    classify,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_FLEET_TOML = REPO_ROOT / ".fleet.toml"


# --------------------------------------------------------------- fail-safe core

def _cfg(*rules: Rule, **kw: object) -> E2EConfig:
    """A 'declared' config from explicit rules for mechanism tests."""
    return E2EConfig(rules=list(rules), source="declared", **kw)  # type: ignore[arg-type]


def test_missing_table_routes_full() -> None:
    cfg = E2EConfig(rules=[], source="missing")
    r = classify(["app_web/static/x.svg"], cfg)
    assert r.tier == "full"
    assert "fail-safe" in r.reasons[0]


def test_empty_table_routes_full() -> None:
    cfg = E2EConfig(rules=[], source="empty")
    assert classify(["src/foo.py"], cfg).tier == "full"


def test_invalid_toml_routes_full() -> None:
    cfg = E2EConfig(rules=[], source="invalid")
    assert classify(["README.md"], cfg).tier == "full"


def test_empty_diff_routes_full() -> None:
    cfg = _cfg(Rule(tier=Category.NONE, prefix="src/"))
    r = classify([], cfg)
    assert r.tier == "full"
    assert "empty-diff" in r.reasons[0]


def test_unmatched_path_is_fail_safe_full() -> None:
    # A path matching no declared rule must escalate to full, not fall to skip.
    cfg = _cfg(Rule(tier=Category.NONE, prefix="src/"))
    r = classify(["totally/unknown/thing.xyz"], cfg)
    assert r.tier == "full"
    assert any("unclassified" in reason for reason in r.reasons)


# --------------------------------------------------------------- tier maths

def test_static_only_routes_static() -> None:
    cfg = _cfg(
        Rule(tier=Category.STATIC, prefix="static/", extensions=("svg",)),
        static_pytest_target="tests/e2e/test_smoke.py",
        static_browsers=("chromium",),
    )
    r = classify(["static/a.svg"], cfg)
    assert r.tier == "static"
    assert r.pytest_target == "tests/e2e/test_smoke.py"
    assert r.browsers == ["chromium"]


def test_none_only_routes_skip() -> None:
    cfg = _cfg(Rule(tier=Category.NONE, prefix="src/", extensions=("py",)))
    r = classify(["src/a.py"], cfg)
    assert r.tier == "skip"
    assert r.pytest_target == ""


def test_mixed_static_and_full_takes_full() -> None:
    cfg = _cfg(
        Rule(tier=Category.STATIC, prefix="static/", extensions=("svg",)),
        Rule(tier=Category.FULL, prefix="app_web/"),
    )
    assert classify(["static/a.svg", "app_web/x.py"], cfg).tier == "full"


def test_mixed_none_and_static_takes_static() -> None:
    cfg = _cfg(
        Rule(tier=Category.STATIC, prefix="static/", extensions=("svg",)),
        Rule(tier=Category.NONE, prefix="src/"),
    )
    assert classify(["static/a.svg", "src/x.py"], cfg).tier == "static"


def test_first_match_wins() -> None:
    # The more-specific STATIC rule is declared first, so an html under the
    # vendored dir is STATIC even though a later FULL rule also prefix-matches.
    cfg = _cfg(
        Rule(tier=Category.STATIC, prefix="app_web/static/_vendored/", extensions=("html",)),
        Rule(tier=Category.FULL, prefix="app_web/static/"),
    )
    assert classify(["app_web/static/_vendored/nav.html"], cfg).tier == "static"
    # ...but a .css under the same vendored dir misses the html-only STATIC rule
    # and falls through to the FULL prefix rule.
    assert classify(["app_web/static/_vendored/nav.css"], cfg).tier == "full"


def test_bare_rule_matches_nothing() -> None:
    # A rule with neither prefix/path/extensions must not match everything.
    cfg = _cfg(Rule(tier=Category.NONE))
    # 'src/a.py' matches no real rule -> fail-safe full, not skip.
    assert classify(["src/a.py"], cfg).tier == "full"


def test_backslash_paths_normalized() -> None:
    cfg = _cfg(Rule(tier=Category.NONE, prefix="src/", extensions=("py",)))
    assert classify(["src\\a.py"], cfg).tier == "skip"


# ------------------------------------------------ anti-drift guard (real .fleet.toml)

def test_real_fleet_toml_declares_usable_e2e_table() -> None:
    cfg = load_config(REAL_FLEET_TOML)
    assert cfg.source == "declared", (
        "this repo's .fleet.toml must declare a usable [e2e] table"
    )
    assert cfg.rules, "at least one [[e2e.rule]] must be declared"


def test_real_rules_route_representative_paths() -> None:
    """Representative paths land in the tier their rule intends.

    Fails loudly if a future edit to the .fleet.toml [e2e] block silently
    under-routes a real e2e surface (the anti-drift requirement in #180).
    """
    cfg = load_config(REAL_FLEET_TOML)

    def tier(*paths: str) -> str:
        return classify(list(paths), cfg).tier

    # Real browser surface -> full.
    assert tier("app_web/static/_vendored/card/card.css") == "full"
    assert tier("app_web/static/_vendored/nav/nav-tabs.js") == "full"
    assert tier("app_web/routers/models.py") == "full"
    assert tier("app_web/static/api.js") == "full"
    assert tier("tray/tray_app.py") == "full"
    assert tier("tests/e2e/test_smoke.py") == "full"

    # Inert static assets -> static.
    assert tier("app_web/static/icon-192.png") == "static"
    assert tier("app_web/static/_vendored/nav/nav-tabs.html") == "static"

    # Backend / tooling / docs -> skip.
    assert tier("src/model_registry.py") == "skip"
    assert tier("tests/test_config.py") == "skip"
    assert tier("scripts/download_models.py") == "skip"
    assert tier("config/models.yaml") == "skip"
    assert tier("launchers/openai_console.py") == "skip"
    assert tier("README.md") == "skip"
    assert tier(".fleet.toml") == "skip"

    # Mixed real diff (static asset + backend) -> static; add a page -> full.
    assert tier("app_web/static/icon-192.png", "src/x.py") == "static"
    assert tier("app_web/static/icon-192.png", "app_web/routers/models.py") == "full"
