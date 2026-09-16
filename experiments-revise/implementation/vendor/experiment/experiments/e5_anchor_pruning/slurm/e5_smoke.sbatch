#!/usr/bin/env bash
#SBATCH --job-name=msi-e5-smoke
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
set -euo pipefail
: "${E5_ROOT:?E5_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
mkdir -p "$RUN_DIR"/{datasets,raw,results,logs,control}
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" make-plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" prepare-dataset \
  --config "$RUN_DIR/config.json" --dataset-dir "$RUN_DIR/datasets" | tee "$RUN_DIR/dataset_summary.json"
CASE_COUNT=$("$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind cases)
for ((CASE_INDEX=0; CASE_INDEX<CASE_COUNT; CASE_INDEX++)); do
  echo "SMOKE_CASE_INDEX=$CASE_INDEX/$((CASE_COUNT-1))"
  "$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" run-index \
    --plan "$RUN_DIR/plan.jsonl" --index "$CASE_INDEX" \
    --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
done
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" validate --results "$RUN_DIR/results"
"$E5_ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
echo "SMOKE_PASS run_dir=$RUN_DIR"
