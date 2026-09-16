#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$ROOT/runs/local-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{raw,results,logs}
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl" \
  | tee "$RUN_DIR/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" run-plan \
  --plan "$RUN_DIR/plan.jsonl" --output "$RUN_DIR/raw" \
  --launcher "$ROOT/exp02_query.pyz" | tee "$RUN_DIR/logs/run.log"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" analyze \
  --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" validate --results "$RUN_DIR/results"
echo 'NOTE: local smoke results validate the artifact only; they are not publication measurements.'
echo "RUN_DIR=$RUN_DIR"
