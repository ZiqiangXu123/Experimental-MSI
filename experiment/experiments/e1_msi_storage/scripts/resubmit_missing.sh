#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$(cat "$ROOT/LAST_RUN_DIR" 2>/dev/null || true)}
PLAN=${2:-$RUN_DIR/plan.jsonl}
if [[ -z "$RUN_DIR" || ! -f "$PLAN" ]]; then
  echo 'usage: resubmit_missing.sh RUN_DIR [PLAN]' >&2
  exit 64
fi
mkdir -p "$RUN_DIR"/{raw,results,logs}
missing=$("$ROOT/scripts/run_python.sh" "$ROOT/scripts/missing_indices.py" "$PLAN" "$RUN_DIR/raw")
if [[ -z "$missing" ]]; then
  echo 'No missing or invalid points. Running completeness analysis directly.'
  "$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" analyze \
    --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$PLAN"
  "$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" validate --results "$RUN_DIR/results"
  exit 0
fi
ARRAY_CONCURRENCY=${ARRAY_CONCURRENCY:-4}
source "$ROOT/scripts/slurm_options.sh"
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="${missing}%${ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/recovery-%A_%a.out" \
  --error="$RUN_DIR/logs/recovery-%A_%a.err" \
  "$ROOT/slurm/e1_point.sbatch" "$ROOT" "$RUN_DIR" "$PLAN")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/recovery-analyze-%j.out" \
  --error="$RUN_DIR/logs/recovery-analyze-%j.err" \
  "$ROOT/slurm/e1_analyze.sbatch" "$ROOT" "$RUN_DIR")
printf '%s\n' "$ARRAY_JOB" > "$RUN_DIR/recovery_array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/recovery_analyze_job_id.txt"
echo "MISSING_INDICES=$missing"
echo "RECOVERY_ARRAY_JOB_ID=$ARRAY_JOB"
echo "RECOVERY_ANALYZE_JOB_ID=$ANALYZE_JOB"
