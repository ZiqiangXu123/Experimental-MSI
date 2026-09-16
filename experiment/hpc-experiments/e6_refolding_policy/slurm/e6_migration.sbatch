#!/usr/bin/env bash
#SBATCH --job-name=e6-migrate
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=6G
#SBATCH --time=04:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}" "${SLURM_ARRAY_TASK_ID:?}"
export E6_SHARED_WORK_BASE="${E6_SHARED_WORK_BASE:-$RUN_DIR/shared-work}"
mkdir -p "$RUN_DIR/raw_migration" "$E6_SHARED_WORK_BASE"
WORK_ROOT="${SLURM_TMPDIR:-$RUN_DIR/shared-work/fallback-${SLURM_JOB_ID}-${SLURM_ARRAY_TASK_ID}}"
mkdir -p "$WORK_ROOT"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" run-migration-block \
  --plan "$RUN_DIR/migration_plan.jsonl" \
  --blocks "$RUN_DIR/migration_blocks.jsonl" \
  --block-index "$SLURM_ARRAY_TASK_ID" \
  --datasets "$RUN_DIR/datasets" \
  --raw "$RUN_DIR/raw_migration" \
  --work-root "$WORK_ROOT"
