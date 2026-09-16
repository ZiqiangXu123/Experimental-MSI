#!/usr/bin/env bash
#SBATCH --job-name=msi-e3-smoke
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8192
#SBATCH --time=00:30:00

set -euo pipefail
ROOT=${1:?usage: e3_smoke.sbatch ROOT RUN_DIR}
RUN_DIR=${2:?usage: e3_smoke.sbatch ROOT RUN_DIR}
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
