# Local pre-ship verification gate.
#
# Runs the same checks the CI workflow runs, so a green local run
# means a green CI run (modulo runner OS quirks). Use before pushing
# to `main` or before opening a PR.
#
# Steps:
#   1. byte-compile every .py file under src/, app_web/, tray/, scripts/
#   2. static ratchet: no new duplicate block (jscpd via npx) or unused
#      import/variable (ruff F401,F841) or curated ruff bug (B) / security (S, S113
#      strict) finding beyond .static-ratchet-baseline.json.
#      Whole tree, never routed; exit 3 (a detector could not run) is NOT
#      CONFIRMED and fails, never a pass or a skip.
#   3. docs ratchet: README.md within its size budget and no new history marker
#      (issue reference, dated or changelog heading) in README.md / docs/**/*.md
#      beyond .docs-ratchet-baseline.json. Whole tree, never routed; exit 3
#      (a file could not be read) is NOT CONFIRMED and fails.
#   4. comment ratchet: no new issue-number reference in a src/ app_web/
#      scripts/ tray/ launchers/ comment or docstring beyond
#      .comment-ratchet-baseline.json. Exit 3 (a file could not be read) is
#      NOT CONFIRMED and fails.
#   5. structure ratchet: no new file over 1,000 lines, function over 100 lines
#      or directory over 40 modules, no baselined one grown, and the root
#      folders inside the `service` shape (.fleet.toml), against
#      .structure-ratchet-baseline.json. Any non-zero exit fails, so exit 3
#      (git unusable, an unparseable file, an unknown shape) is NOT CONFIRMED
#      and never a pass.
#   6. instruction ratchet: no rise in a weekly-prompt-audit rule hit per
#      instruction file (CLAUDE.md, AGENTS.md, rules, skills) and no file past
#      its size cap, against .instruction-ratchet-baseline.json. Whole tree,
#      never routed. Any non-zero exit fails, so exit 3 (git unusable, an
#      unreadable file) is NOT CONFIRMED and never a pass.
#   7. doc references ratchet: no README.md / CLAUDE.md / docs/**/*.md reference
#      to a repo path, `python -m` module, relative link or loopback port that
#      does not exist, beyond .doc-refs-ratchet-baseline.json. Whole tree, never
#      routed. Any non-zero exit fails, so exit 3 (git unusable, an unreadable
#      file) is NOT CONFIRMED and never a pass.
#   8. dead code ratchet: no function, class or method that nothing in the tracked
#      Python tree refers to, beyond .dead-code-ratchet-baseline.json. Whole tree,
#      never routed (deleting the last caller in one file strands a definition in
#      another). Any non-zero exit fails, so exit 3 (git unusable, a file that does
#      not parse) is NOT CONFIRMED and never a pass.
#   9. conventions ratchet: no rise in a fleet-convention hit (no-window,
#      logging-not-print, no-drive-path) per file beyond
#      .conventions-ratchet-baseline.json. Whole tree, never routed. Any
#      non-zero exit fails, so exit 3 (git unusable, a file that does not
#      parse) is NOT CONFIRMED and never a pass.
#  10. `pytest -q` (unit tests, no GPU / no real CLIs)
#  11. e2e, routed by scripts/classify_e2e.py against the [e2e] table in
#      .fleet.toml: nothing for a diff with no browser surface, the smoke
#      target for inert assets, a surface slice or the full suite otherwise.
#      An unclassified path, an empty diff or a router failure runs the
#      full suite and prints why.
#
# Stops on the first failure.
#
# -Classifier   router script to ask (default scripts\classify_e2e.py); a
#               seam so the fail-safe paths can be exercised with a stub.
# -RouteOnly    print the e2e routing decision and exit, running no stage.

param(
    [string]$Classifier = "scripts\classify_e2e.py",
    [switch]$RouteOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "❌ .venv\Scripts\python.exe not found at $Python" -ForegroundColor Red
    exit 1
}

function Step($label, $block) {
    Write-Host ""
    Write-Host "▶ $label" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ $label failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "✅ $label" -ForegroundColor Green
}

# Ask the router what the diff needs. Every way of not getting a usable answer
# (non-zero exit, no E2E_TIER line, an unknown tier, no target) resolves to the
# full suite with the reason printed -- uncertainty never narrows what runs.
function Get-E2ERoute {
    $route = @{ Tier = "full"; Target = "tests/e2e"; Browsers = "chromium"; Reason = "" }
    $out = & $Python $Classifier
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $route.Reason = "router failed (exit $code) -- running the full suite (fail-safe)"
        return $route
    }
    $kv = @{}
    foreach ($line in $out) {
        if ($line -match '^(E2E_[A-Z_]+)=(.*)$') { $kv[$matches[1]] = $matches[2] }
    }
    $tier = $kv["E2E_TIER"]
    if (-not $tier) {
        $route.Reason = "router gave no E2E_TIER verdict -- running the full suite (fail-safe)"
    } elseif ($tier -notin @("skip", "static", "surface", "full")) {
        $route.Reason = "router gave unknown tier '$tier' -- running the full suite (fail-safe)"
    } elseif ($tier -ne "skip" -and -not $kv["E2E_PYTEST_TARGET"]) {
        $route.Reason = "router gave tier '$tier' without a pytest target -- running the full suite (fail-safe)"
    } else {
        $route.Tier = $tier
        $route.Target = $kv["E2E_PYTEST_TARGET"]
        $route.Browsers = $kv["E2E_BROWSERS"]
        $route.Reason = $kv["E2E_REASON"]
    }
    return $route
}

$e2e = Get-E2ERoute
if ($RouteOnly) {
    Write-Host "E2E_ROUTE tier=$($e2e.Tier) target=$($e2e.Target) browsers=$($e2e.Browsers)"
    Write-Host "E2E_ROUTE reason=$($e2e.Reason)"
    exit 0
}

Step "byte-compile" {
    & $Python -m compileall -q src app_web tray scripts
}

Step "static ratchet" {
    # The wrapper maps the script's exit 3 to NOT CONFIRMED
    # (tests/ratchets/test_static_ratchet_gate.py pins it); call it, never
    # scripts/static_ratchet.py directly.
    & $Python -m scripts.static_ratchet_gate
}

Step "docs ratchet" {
    # Same wrapper shape as the static ratchet: exit 3 (a file could not be
    # read) is NOT CONFIRMED and fails (tests/ratchets/test_docs_ratchet_gate.py pins it).
    & $Python -m scripts.docs_ratchet_gate
}

Step "comment ratchet" {
    # Same wrapper shape as the static ratchet: exit 3 (a file could not be
    # read) is NOT CONFIRMED and fails (tests/ratchets/test_comment_ratchet_gate.py pins it).
    & $Python -m scripts.comment_ratchet_gate
}

Step "structure ratchet" {
    # Called directly: Step fails on any non-zero exit, so exit 3 (not
    # confirmed) stops the gate like a finding does.
    & $Python scripts\structure_ratchet.py
}

Step "instruction ratchet" {
    # Called directly, like the structure ratchet: Step fails on any non-zero
    # exit, so exit 3 (not confirmed) stops the gate like a finding does.
    & $Python scripts\instruction_ratchet.py
}

Step "doc refs ratchet" {
    # Called directly, like the instruction ratchet: Step fails on any non-zero
    # exit, so exit 3 (not confirmed) stops the gate like a finding does.
    & $Python scripts\doc_refs_ratchet.py
}

Step "dead code ratchet" {
    # Called directly, like the doc refs ratchet: Step fails on any non-zero
    # exit, so exit 3 (not confirmed) stops the gate like a finding does.
    & $Python scripts\dead_code_ratchet.py
}

Step "conventions ratchet" {
    # Called directly, like the dead code ratchet: Step fails on any non-zero
    # exit, so exit 3 (not confirmed) stops the gate like a finding does
    # (tests/ratchets/test_conventions_ratchet_stage.py pins it).
    & $Python scripts\conventions_ratchet.py
}

Step "pytest (unit)" {
    & $Python -m pytest -q --ignore=tests/e2e
}

if ($e2e.Tier -eq "skip") {
    Write-Host ""
    Write-Host "▶ e2e routing: SKIP browser suite (no e2e surface touched)" -ForegroundColor Cyan
    Write-Host "   reason: $($e2e.Reason)" -ForegroundColor DarkGray
    Write-Host "✅ pytest (e2e) skipped, diff touches no e2e surface" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "▶ e2e routing: $($e2e.Tier)" -ForegroundColor Cyan
    Write-Host "   reason: $($e2e.Reason)" -ForegroundColor DarkGray
    # A surface slice names several targets space-separated; pytest needs
    # each as its own argument.
    $e2eArgs = @($e2e.Target -split '\s+' | Where-Object { $_ }) + @("-q")
    foreach ($b in ($e2e.Browsers -split ',' | Where-Object { $_ })) {
        $e2eArgs += @("--browser", $b)
    }
    $label = if ($e2e.Browsers) { $e2e.Browsers } else { "suite-default" }
    Step "pytest (e2e · $($e2e.Tier): $($e2e.Target), $label)" {
        & $Python -m pytest @e2eArgs
    }
}

Write-Host ""
Write-Host "🚀 all green" -ForegroundColor Green
