#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$ROOT/local-smoke}
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"/{raw,results}
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl"
blocks=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$RUN_DIR/plan.jsonl" --kind blocks)
for ((i=0; i<blocks; i++)); do
  "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" run-block \
    --plan "$RUN_DIR/plan.jsonl" --index "$i" --output "$RUN_DIR/raw" \
    --launcher "$ROOT/exp03_network.pyz"
done
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" analyze \
  --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" validate --results "$RUN_DIR/results"
(cd "$RUN_DIR/results" && sha256sum -c manifest.sha256)
echo "RUN_DIR=$RUN_DIR"
