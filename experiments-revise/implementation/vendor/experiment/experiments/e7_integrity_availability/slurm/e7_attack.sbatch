#!/usr/bin/env bash
#SBATCH --job-name=e7-integrity
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=04:00:00
set -euo pipefail
: "${EXP07_ROOT:?}" "${RUN_DIR:?}" "${SLURM_ARRAY_TASK_ID:?}"
mkdir -p "$RUN_DIR/raw_e7"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" run-e7-block \
  --plan-dir "$RUN_DIR" --index "$SLURM_ARRAY_TASK_ID" --raw "$RUN_DIR/raw_e7"
