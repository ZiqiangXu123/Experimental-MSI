#!/usr/bin/env bash
#SBATCH --job-name=msi-e1-analyze
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2048
#SBATCH --time=00:30:00

set -euo pipefail
ROOT=${1:?usage: e1_analyze.sbatch ROOT RUN_DIR}
RUN_DIR=${2:?usage: e1_analyze.sbatch ROOT RUN_DIR}
mkdir -p "$RUN_DIR/results"

echo "START $(date -Is 2>/dev/null || date)"
plan_args=(--plan "$RUN_DIR/plan.jsonl")
if [[ -f "$RUN_DIR/large_scale_plan.jsonl" ]]; then
  plan_args+=(--plan "$RUN_DIR/large_scale_plan.jsonl")
fi
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" analyze \
  --raw "$RUN_DIR/raw" \
  --output "$RUN_DIR/results" \
  "${plan_args[@]}"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" validate \
  --results "$RUN_DIR/results"
echo "END $(date -Is 2>/dev/null || date)"
