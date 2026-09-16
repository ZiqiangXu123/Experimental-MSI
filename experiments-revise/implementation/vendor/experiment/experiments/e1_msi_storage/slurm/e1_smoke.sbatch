#!/usr/bin/env bash
#SBATCH --job-name=msi-e1-smoke
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2048
#SBATCH --time=00:30:00

set -euo pipefail
ROOT=${1:?usage: e1_smoke.sbatch ROOT RUN_DIR}
RUN_DIR=${2:?usage: e1_smoke.sbatch ROOT RUN_DIR}
mkdir -p "$RUN_DIR"/{raw,results,logs}

echo "START $(date -Is 2>/dev/null || date)"
echo "HOST $(hostname -f 2>/dev/null || hostname)"
echo "JOB ${SLURM_JOB_ID:-none}"

"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" \
  --output "$RUN_DIR/plan.jsonl"

scratch_args=()
if [[ -n "${EXP01_SCRATCH:-}" ]]; then
  scratch_args=(--scratch "$EXP01_SCRATCH")
fi
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" run-plan \
  --plan "$RUN_DIR/plan.jsonl" \
  --output "$RUN_DIR/raw" \
  "${scratch_args[@]}"

"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" analyze \
  --raw "$RUN_DIR/raw" \
  --output "$RUN_DIR/results" \
  --plan "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" validate \
  --results "$RUN_DIR/results"

echo "END $(date -Is 2>/dev/null || date)"
