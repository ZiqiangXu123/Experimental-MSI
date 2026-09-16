#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e6-smoke-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{logs,control}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/smoke.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp06_refolding_policy.pyz" "$RUN_DIR/config.json" > "$RUN_DIR/software.sha256"
JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --output="$RUN_DIR/logs/smoke-%j.out" --error="$RUN_DIR/logs/smoke-%j.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
  "$ROOT/slurm/e6_smoke.sbatch")
printf '%s\n' "$JOB" > "$RUN_DIR/smoke_job_id.txt"
printf 'RUN_DIR=%s\nSMOKE_JOB_ID=%s\n' "$RUN_DIR" "$JOB"
