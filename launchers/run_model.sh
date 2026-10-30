#!/usr/bin/env bash
# local-llm-hub - parameterized per-model launcher.
# Execs one model's backend via `run_backend`, keyed by model id so the
# title/port/banner come live from config/models.yaml instead of being
# hardcoded per model.
#
# Usage: launchers/run_model.sh <model-id>   (id from config/models.yaml)
set -euo pipefail
if [ -z "${1:-}" ]; then
  echo "usage: run_model.sh <model-id>" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m src.run_backend "$1"
