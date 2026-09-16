#!/usr/bin/env bash
#SBATCH --job-name=msi-e5-analyze
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=01:00:00
set -euo pipefail
: "${E5_ROOT:?E5_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" inspect-raw \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw"
rm -rf "$RUN_DIR/results"
mkdir -p "$RUN_DIR/results"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" validate --results "$RUN_DIR/results"
"$E5_ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
echo "ANALYSIS_PASS run_dir=$RUN_DIR"
