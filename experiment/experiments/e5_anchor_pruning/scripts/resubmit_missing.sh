#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: resubmit_missing.sh RUN_DIR}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
[[ -f "$RUN_DIR/plan.jsonl" && -f "$RUN_DIR/config.json" ]] || { echo 'ERROR: run plan/config is missing.' >&2; exit 66; }
INDICES=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" missing-indices --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw")
if [[ -z "$INDICES" ]]; then
  echo 'No missing or corrupt case results. Submitting analysis only.'
  ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
    --output="$RUN_DIR/logs/reanalyze-%j.out" --error="$RUN_DIR/logs/reanalyze-%j.err" \
    --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e5_analyze.sbatch")
  printf '%s\n' "$ANALYZE_JOB" >> "$RUN_DIR/recovery_analyze_job_ids.txt"
  echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
  exit 0
fi
DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --output="$RUN_DIR/logs/recovery-dataset-%j.out" --error="$RUN_DIR/logs/recovery-dataset-%j.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e5_prepare_dataset.sbatch")
DATASET_JOB_BASE=${DATASET_JOB%%;*}
EXTRA=()
[[ "${E5_REQUEST_EXCLUSIVE:-0}" == 1 ]] && EXTRA+=(--exclusive)
CASE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${EXTRA[@]}" \
  --dependency="afterok:${DATASET_JOB_BASE}" --array="${INDICES}%${ARRAY_CONCURRENCY:-8}" \
  --output="$RUN_DIR/logs/recovery-case-%A_%a.out" --error="$RUN_DIR/logs/recovery-case-%A_%a.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E5_DISABLE_PINNING="${E5_DISABLE_PINNING:-0}" \
  "$ROOT/slurm/e5_case.sbatch")
CASE_JOB_BASE=${CASE_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${CASE_JOB_BASE}" \
  --output="$RUN_DIR/logs/reanalyze-%j.out" --error="$RUN_DIR/logs/reanalyze-%j.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e5_analyze.sbatch")
printf '%s\n' "$DATASET_JOB" >> "$RUN_DIR/recovery_dataset_job_ids.txt"
printf '%s\n' "$CASE_JOB" >> "$RUN_DIR/recovery_case_job_ids.txt"
printf '%s\n' "$ANALYZE_JOB" >> "$RUN_DIR/recovery_analyze_job_ids.txt"
printf 'MISSING_INDICES=%s\nDATASET_JOB_ID=%s\nCASE_JOB_ID=%s\nANALYZE_JOB_ID=%s\n' \
  "$INDICES" "$DATASET_JOB" "$CASE_JOB" "$ANALYZE_JOB"
