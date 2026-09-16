#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e7-publication-$(date +%Y%m%d-%H%M%S)}
E7_ARRAY_CONCURRENCY=${E7_ARRAY_CONCURRENCY:-8}
E8_ARRAY_CONCURRENCY=${E8_ARRAY_CONCURRENCY:-16}
mkdir -p "$RUN_DIR"/{raw_e7,raw_e8,results,logs,control}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/publication.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR" | tee "$RUN_DIR/plan_submission.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp07_security_availability.pyz" "$RUN_DIR/config.json" \
  "$RUN_DIR/e7_cases.jsonl" "$RUN_DIR/e7_blocks.jsonl" \
  "$RUN_DIR/e8_cases.jsonl" "$RUN_DIR/e8_blocks.jsonl" > "$RUN_DIR/software_and_plan.sha256"
E7_BLOCKS=$(wc -l < "$RUN_DIR/e7_blocks.jsonl")
E8_BLOCKS=$(wc -l < "$RUN_DIR/e8_blocks.jsonl")
[[ "$E7_BLOCKS" -eq 247 && "$E8_BLOCKS" -eq 360 ]] || {
  echo "ERROR: publication plan changed (E7 blocks=$E7_BLOCKS; E8 blocks=$E8_BLOCKS)." >&2
  exit 65
}
E7_EXTRA=()
[[ "${E7_REQUEST_EXCLUSIVE:-0}" == 1 ]] && E7_EXTRA+=(--exclusive)
E7_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${E7_EXTRA[@]}" \
  --array="0-$((E7_BLOCKS-1))%${E7_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/e7-%A_%a.out" --error="$RUN_DIR/logs/e7-%A_%a.err" \
  --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e7_attack.sbatch")
E7_BASE=${E7_JOB%%;*}
E8_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-$((E8_BLOCKS-1))%${E8_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/e8-%A_%a.out" --error="$RUN_DIR/logs/e8-%A_%a.err" \
  --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e8_availability.sbatch")
E8_BASE=${E8_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${E7_BASE}:${E8_BASE}" \
  --output="$RUN_DIR/logs/analyze-%j.out" --error="$RUN_DIR/logs/analyze-%j.err" \
  --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e7_analyze.sbatch")
printf '%s\n' "$E7_JOB" > "$RUN_DIR/e7_job_id.txt"
printf '%s\n' "$E8_JOB" > "$RUN_DIR/e8_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_job_id.txt"
printf 'RUN_DIR=%s\nE7_JOB_ID=%s\nE8_JOB_ID=%s\nANALYZE_JOB_ID=%s\n' "$RUN_DIR" "$E7_JOB" "$E8_JOB" "$ANALYZE_JOB"
