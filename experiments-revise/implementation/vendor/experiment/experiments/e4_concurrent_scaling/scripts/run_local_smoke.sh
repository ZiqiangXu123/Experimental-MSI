#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-${TMPDIR:-/tmp}/e4-local-smoke-$USER-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{datasets,raw,results,logs,control}
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" prepare-all \
  --plan "$RUN_DIR/plan.jsonl" --dataset-dir "$RUN_DIR/datasets" --workers "${DATASET_WORKERS:-2}"
CASE_COUNT=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind cases)
for ((CASE_INDEX=0; CASE_INDEX<CASE_COUNT; CASE_INDEX++)); do
  echo "SMOKE_CASE_INDEX=$CASE_INDEX/$((CASE_COUNT-1))"
  "$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" run-index \
    --plan "$RUN_DIR/plan.jsonl" --index "$CASE_INDEX" \
    --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
done
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" validate --results "$RUN_DIR/results"
"$ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
printf 'RUN_DIR=%s\n' "$RUN_DIR"
