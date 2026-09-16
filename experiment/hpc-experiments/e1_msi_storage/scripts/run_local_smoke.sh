#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$ROOT/runs/local-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{raw,results}
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" make-plan --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" run-plan --plan "$RUN_DIR/plan.jsonl" --output "$RUN_DIR/raw"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" analyze --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" validate --results "$RUN_DIR/results"
echo "RUN_DIR=$RUN_DIR"
