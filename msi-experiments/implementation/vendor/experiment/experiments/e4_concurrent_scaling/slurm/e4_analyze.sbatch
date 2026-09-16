#!/usr/bin/env bash
#SBATCH --job-name=msi-e4-analyze
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=01:00:00
set -euo pipefail
: "${E4_ROOT:?E4_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" inspect-raw \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" validate --results "$RUN_DIR/results"
"$E4_ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
echo "ANALYSIS_PASS run_dir=$RUN_DIR"
