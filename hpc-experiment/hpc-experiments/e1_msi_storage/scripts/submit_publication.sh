#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e1-publication-$(date +%Y%m%d-%H%M%S)}
ARRAY_CONCURRENCY=${ARRAY_CONCURRENCY:-4}
mkdir -p "$RUN_DIR"/{raw,results,logs}

"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" make-plan \
  --config "$ROOT/configs/publication.json" \
  --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.txt"
POINTS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" count-plan --plan "$RUN_DIR/plan.jsonl")
LAST=$((POINTS - 1))
source "$ROOT/scripts/slurm_options.sh"
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-${LAST}%${ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/point-%A_%a.out" \
  --error="$RUN_DIR/logs/point-%A_%a.err" \
  "$ROOT/slurm/e1_point.sbatch" "$ROOT" "$RUN_DIR")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/analyze-%j.out" \
  --error="$RUN_DIR/logs/analyze-%j.err" \
  "$ROOT/slurm/e1_analyze.sbatch" "$ROOT" "$RUN_DIR")
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
printf '%s\n' "$ARRAY_JOB" > "$RUN_DIR/array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_job_id.txt"
echo "RUN_DIR=$RUN_DIR"
echo "POINTS=$POINTS"
echo "ARRAY_JOB_ID=$ARRAY_JOB"
echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
echo "Monitor: squeue -j ${ARRAY_ID},${ANALYZE_JOB%%;*}"
