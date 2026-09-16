#!/usr/bin/env bash
#SBATCH --job-name=e6-dataset
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=6G
#SBATCH --time=04:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}" "${SLURM_ARRAY_TASK_ID:?}"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" build-dataset \
  --plan "$RUN_DIR/dataset_plan.jsonl" \
  --datasets "$RUN_DIR/datasets" \
  --index "$SLURM_ARRAY_TASK_ID"
