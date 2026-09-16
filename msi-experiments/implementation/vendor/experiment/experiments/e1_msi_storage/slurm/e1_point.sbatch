#!/usr/bin/env bash
#SBATCH --job-name=msi-e1-point
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4096
#SBATCH --time=02:00:00

set -euo pipefail
ROOT=${1:?usage: e1_point.sbatch ROOT RUN_DIR}
RUN_DIR=${2:?usage: e1_point.sbatch ROOT RUN_DIR [PLAN]}
PLAN=${3:-$RUN_DIR/plan.jsonl}
INDEX=${SLURM_ARRAY_TASK_ID:?this script must run as a Slurm array task}
mkdir -p "$RUN_DIR/raw"

echo "START $(date -Is 2>/dev/null || date)"
echo "HOST $(hostname -f 2>/dev/null || hostname)"
echo "ARRAY_JOB ${SLURM_ARRAY_JOB_ID:-none} TASK $INDEX"

scratch_args=()
if [[ -n "${EXP01_SCRATCH:-}" ]]; then
  scratch_args=(--scratch "$EXP01_SCRATCH")
fi
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" run-point \
  --plan "$PLAN" \
  --index "$INDEX" \
  --output "$RUN_DIR/raw" \
  "${scratch_args[@]}"

echo "END $(date -Is 2>/dev/null || date)"
