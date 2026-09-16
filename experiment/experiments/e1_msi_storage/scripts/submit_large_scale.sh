#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$(cat "$ROOT/LAST_RUN_DIR" 2>/dev/null || true)}
if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR/raw" ]]; then
  echo "usage: submit_large_scale.sh EXISTING_PUBLICATION_RUN_DIR" >&2
  exit 64
fi
ARRAY_CONCURRENCY=${ARRAY_CONCURRENCY:-2}
PLAN="$RUN_DIR/large_scale_plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" make-plan \
  --config "$ROOT/configs/large_scale.json" \
  --output "$PLAN" | tee "$RUN_DIR/large_scale_plan_summary.txt"
POINTS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" count-plan --plan "$PLAN")
LAST=$((POINTS - 1))
source "$ROOT/scripts/slurm_options.sh"
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-${LAST}%${ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/large-%A_%a.out" \
  --error="$RUN_DIR/logs/large-%A_%a.err" \
  "$ROOT/slurm/e1_point.sbatch" "$ROOT" "$RUN_DIR" "$PLAN")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/reanalyze-%j.out" \
  --error="$RUN_DIR/logs/reanalyze-%j.err" \
  "$ROOT/slurm/e1_analyze.sbatch" "$ROOT" "$RUN_DIR")
echo "RUN_DIR=$RUN_DIR"
echo "LARGE_ARRAY_JOB_ID=$ARRAY_JOB"
echo "REANALYZE_JOB_ID=$ANALYZE_JOB"
