#!/usr/bin/env bash
#SBATCH --job-name=msi-e4-dataset
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=01:00:00
set -euo pipefail
: "${E4_ROOT:?E4_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
: "${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}"
mkdir -p "$RUN_DIR/control" "$RUN_DIR/datasets"
SPEC_JSON="$RUN_DIR/control/dataset-${SLURM_ARRAY_TASK_ID}.json"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" get-dataset \
  --dataset-plan "$RUN_DIR/dataset_plan.jsonl" \
  --index "$SLURM_ARRAY_TASK_ID" \
  --output "$SPEC_JSON"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" prepare-dataset \
  --spec-json "$SPEC_JSON" \
  --dataset-dir "$RUN_DIR/datasets" \
  --workers "${DATASET_WORKERS:-${SLURM_CPUS_PER_TASK:-1}}"
echo "DATASET_TASK_PASS index=$SLURM_ARRAY_TASK_ID host=$(hostname)"
