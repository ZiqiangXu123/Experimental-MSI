#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$ROOT/runs/e5-local-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{datasets,raw,results,logs,control}
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" prepare-dataset \
  --config "$ROOT/configs/smoke.json" --dataset-dir "$RUN_DIR/datasets" | tee "$RUN_DIR/dataset_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" run-plan \
  --plan "$RUN_DIR/plan.jsonl" --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" validate --results "$RUN_DIR/results"
"$ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
echo "LOCAL_SMOKE_PASS run_dir=$RUN_DIR"
