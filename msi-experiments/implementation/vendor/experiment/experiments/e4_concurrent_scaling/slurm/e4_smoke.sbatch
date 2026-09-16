#!/usr/bin/env bash
#SBATCH --job-name=msi-e4-smoke
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=00:20:00
set -euo pipefail
: "${E4_ROOT:?E4_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
mkdir -p "$RUN_DIR"/{datasets,raw,results,logs,control}
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" make-plan \
  --config "$E4_ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" prepare-all \
  --plan "$RUN_DIR/plan.jsonl" --dataset-dir "$RUN_DIR/datasets" --workers "${DATASET_WORKERS:-2}"
CASE_COUNT=$("$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind cases)
for ((CASE_INDEX=0; CASE_INDEX<CASE_COUNT; CASE_INDEX++)); do
  echo "SMOKE_CASE_INDEX=$CASE_INDEX/$((CASE_COUNT-1))"
  "$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" run-index \
    --plan "$RUN_DIR/plan.jsonl" --index "$CASE_INDEX" \
    --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
done
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" analyze \
  --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw" --output "$RUN_DIR/results"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" validate --results "$RUN_DIR/results"
"$E4_ROOT/scripts/verify_manifest.sh" "$RUN_DIR/results"
echo "SMOKE_PASS run_dir=$RUN_DIR"
