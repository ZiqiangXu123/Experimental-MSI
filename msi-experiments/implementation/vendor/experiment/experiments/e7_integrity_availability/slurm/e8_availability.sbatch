#!/usr/bin/env bash
#SBATCH --job-name=e8-availability
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=03:00:00
set -euo pipefail
: "${EXP07_ROOT:?}" "${RUN_DIR:?}" "${SLURM_ARRAY_TASK_ID:?}"
mkdir -p "$RUN_DIR/raw_e8"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" run-e8-block \
  --plan-dir "$RUN_DIR" --index "$SLURM_ARRAY_TASK_ID" --raw "$RUN_DIR/raw_e8"
