#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: resubmit_missing.sh RUN_DIR}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
[[ -f "$RUN_DIR/plan.jsonl" && -f "$RUN_DIR/dataset_plan.jsonl" ]] || { echo 'ERROR: run plan files are missing.' >&2; exit 66; }
INDICES=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" missing-indices --plan "$RUN_DIR/plan.jsonl" --raw "$RUN_DIR/raw")
TOPOLOGY=$(cat "$RUN_DIR/topology.txt" 2>/dev/null || echo portable-single-node-separate-processes)
if [[ -z "$INDICES" ]]; then
  echo 'No missing or corrupt case results. Submitting analysis only.'
  ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
    --output="$RUN_DIR/logs/reanalyze-%j.out" --error="$RUN_DIR/logs/reanalyze-%j.err" \
    --export=ALL,E4_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e4_analyze.sbatch")
  printf '%s\n' "$ANALYZE_JOB" >> "$RUN_DIR/recovery_analyze_job_ids.txt"
  echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
  exit 0
fi
DATASETS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" count-plan --plan "$RUN_DIR/plan.jsonl" --kind datasets)
DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-$((DATASETS-1))%${DATASET_ARRAY_CONCURRENCY:-2}" \
  --output="$RUN_DIR/logs/recovery-dataset-%A_%a.out" --error="$RUN_DIR/logs/recovery-dataset-%A_%a.err" \
  --export=ALL,E4_ROOT="$ROOT",RUN_DIR="$RUN_DIR",DATASET_WORKERS="${DATASET_WORKERS:-4}" \
  "$ROOT/slurm/e4_prepare_dataset.sbatch")
DATASET_JOB_BASE=${DATASET_JOB%%;*}
if [[ "$TOPOLOGY" == dual-node-distinct-processes ]]; then
  CASE_SCRIPT="$ROOT/slurm/e4_case_dual.sbatch"
  CONCURRENCY=${DUAL_ARRAY_CONCURRENCY:-1}
  EXTRA=()
  [[ "${E4_REQUEST_EXCLUSIVE:-0}" == 1 ]] && EXTRA+=(--exclusive)
else
  CASE_SCRIPT="$ROOT/slurm/e4_case_single.sbatch"
  CONCURRENCY=${ARRAY_CONCURRENCY:-4}
  EXTRA=()
  [[ "${E4_REQUEST_EXCLUSIVE:-0}" == 1 ]] && EXTRA+=(--exclusive)
fi
CASE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${EXTRA[@]}" \
  --dependency="afterok:${DATASET_JOB_BASE}" --array="${INDICES}%${CONCURRENCY}" \
  --output="$RUN_DIR/logs/recovery-case-%A_%a.out" --error="$RUN_DIR/logs/recovery-case-%A_%a.err" \
  --export=ALL,E4_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E4_DISABLE_PINNING="${E4_DISABLE_PINNING:-0}" \
  "$CASE_SCRIPT")
CASE_JOB_BASE=${CASE_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${CASE_JOB_BASE}" \
  --output="$RUN_DIR/logs/reanalyze-%j.out" --error="$RUN_DIR/logs/reanalyze-%j.err" \
  --export=ALL,E4_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e4_analyze.sbatch")
printf '%s\n' "$DATASET_JOB" >> "$RUN_DIR/recovery_dataset_job_ids.txt"
printf '%s\n' "$CASE_JOB" >> "$RUN_DIR/recovery_case_job_ids.txt"
printf '%s\n' "$ANALYZE_JOB" >> "$RUN_DIR/recovery_analyze_job_ids.txt"
printf 'MISSING_INDICES=%s\nDATASET_JOB_ID=%s\nCASE_JOB_ID=%s\nANALYZE_JOB_ID=%s\n' \
  "$INDICES" "$DATASET_JOB" "$CASE_JOB" "$ANALYZE_JOB"
