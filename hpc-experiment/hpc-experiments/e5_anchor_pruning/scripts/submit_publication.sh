#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e5-publication-$(date +%Y%m%d-%H%M%S)}
ARRAY_CONCURRENCY=${ARRAY_CONCURRENCY:-8}
mkdir -p "$RUN_DIR"/{datasets,raw,results,logs,control}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/publication.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" make-plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp05_anchor.pyz" "$RUN_DIR/config.json" "$RUN_DIR/plan.jsonl" > "$RUN_DIR/software_and_plan.sha256"
CASES=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" count-plan --plan "$RUN_DIR/plan.jsonl" --kind cases)
[[ "$CASES" -eq 552 ]] || { echo "ERROR: expected 552 publication cases, found $CASES." >&2; exit 65; }
DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --output="$RUN_DIR/logs/dataset-%j.out" --error="$RUN_DIR/logs/dataset-%j.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e5_prepare_dataset.sbatch")
DATASET_JOB_BASE=${DATASET_JOB%%;*}
CASE_OPTIONS=()
[[ "${E5_REQUEST_EXCLUSIVE:-0}" == 1 ]] && CASE_OPTIONS+=(--exclusive)
CASE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${CASE_OPTIONS[@]}" \
  --dependency="afterok:${DATASET_JOB_BASE}" \
  --array="0-$((CASES-1))%${ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/case-%A_%a.out" --error="$RUN_DIR/logs/case-%A_%a.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E5_DISABLE_PINNING="${E5_DISABLE_PINNING:-0}" \
  "$ROOT/slurm/e5_case.sbatch")
CASE_JOB_BASE=${CASE_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${CASE_JOB_BASE}" \
  --output="$RUN_DIR/logs/analyze-%j.out" --error="$RUN_DIR/logs/analyze-%j.err" \
  --export=ALL,E5_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e5_analyze.sbatch")
printf '%s\n' "$DATASET_JOB" > "$RUN_DIR/dataset_job_id.txt"
printf '%s\n' "$CASE_JOB" > "$RUN_DIR/array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_job_id.txt"
printf 'RUN_DIR=%s\nDATASET_JOB_ID=%s\nARRAY_JOB_ID=%s\nANALYZE_JOB_ID=%s\n' \
  "$RUN_DIR" "$DATASET_JOB" "$CASE_JOB" "$ANALYZE_JOB"
