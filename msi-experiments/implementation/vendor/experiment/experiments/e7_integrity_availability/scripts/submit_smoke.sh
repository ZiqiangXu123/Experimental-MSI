#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e7-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{raw_e7,raw_e8,results,logs,control}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/smoke.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp07_security_availability.pyz" "$RUN_DIR/config.json" > "$RUN_DIR/software.sha256"
JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --output="$RUN_DIR/logs/smoke-%j.out" --error="$RUN_DIR/logs/smoke-%j.err" \
  --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e7_smoke.sbatch")
printf '%s\n' "$JOB" > "$RUN_DIR/smoke_job_id.txt"
printf 'RUN_DIR=%s\nSMOKE_JOB_ID=%s\n' "$RUN_DIR" "$JOB"
