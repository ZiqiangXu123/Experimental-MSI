#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e2-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR/logs"
source "$ROOT/scripts/slurm_options.sh"
JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --output="$RUN_DIR/logs/smoke-%j.out" \
  --error="$RUN_DIR/logs/smoke-%j.err" \
  "$ROOT/slurm/e2_smoke.sbatch" "$ROOT" "$RUN_DIR")
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
printf '%s\n' "$JOB" > "$RUN_DIR/smoke_job_id.txt"
echo "RUN_DIR=$RUN_DIR"
echo "SMOKE_JOB_ID=$JOB"
echo "Monitor: squeue -j ${JOB%%;*}"
