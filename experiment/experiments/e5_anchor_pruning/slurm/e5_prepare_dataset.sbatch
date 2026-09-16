#!/usr/bin/env bash
#SBATCH --job-name=msi-e5-dataset
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=01:00:00
set -euo pipefail
: "${E5_ROOT:?E5_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
mkdir -p "$RUN_DIR/datasets" "$RUN_DIR/control"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" prepare-dataset \
  --config "$RUN_DIR/config.json" --dataset-dir "$RUN_DIR/datasets" | tee "$RUN_DIR/dataset_summary.json"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" validate-dataset \
  --config "$RUN_DIR/config.json" --dataset-dir "$RUN_DIR/datasets"
echo "DATASET_PASS host=$(hostname)"
