#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: resubmit_missing.sh RUN_DIR}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
for required in resolved_config.json e7_cases.jsonl e7_blocks.jsonl e8_cases.jsonl e8_blocks.jsonl; do
  [[ -f "$RUN_DIR/$required" ]] || { echo "ERROR: $RUN_DIR/$required is missing." >&2; exit 66; }
done
MISSING_E7=$("$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" missing-blocks \
  --experiment E7 --plan-dir "$RUN_DIR" --raw "$RUN_DIR/raw_e7")
MISSING_E8=$("$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" missing-blocks \
  --experiment E8 --plan-dir "$RUN_DIR" --raw "$RUN_DIR/raw_e8")
DEPENDENCIES=()
if [[ -n "$MISSING_E7" ]]; then
  echo "Missing/corrupt E7 blocks: $MISSING_E7"
  E7_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --array="${MISSING_E7}%${E7_ARRAY_CONCURRENCY:-8}" \
    --output="$RUN_DIR/logs/e7-recovery-%A_%a.out" --error="$RUN_DIR/logs/e7-recovery-%A_%a.err" \
    --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e7_attack.sbatch")
  DEPENDENCIES+=("${E7_JOB%%;*}")
  printf '%s\n' "$E7_JOB" > "$RUN_DIR/e7_recovery_job_id.txt"
fi
if [[ -n "$MISSING_E8" ]]; then
  echo "Missing/corrupt E8 blocks: $MISSING_E8"
  E8_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --array="${MISSING_E8}%${E8_ARRAY_CONCURRENCY:-16}" \
    --output="$RUN_DIR/logs/e8-recovery-%A_%a.out" --error="$RUN_DIR/logs/e8-recovery-%A_%a.err" \
    --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e8_availability.sbatch")
  DEPENDENCIES+=("${E8_JOB%%;*}")
  printf '%s\n' "$E8_JOB" > "$RUN_DIR/e8_recovery_job_id.txt"
fi
rm -rf "$RUN_DIR/results"
mkdir -p "$RUN_DIR/results"
ANALYZE_ARGS=("${SBATCH_SITE_OPTIONS[@]}")
if ((${#DEPENDENCIES[@]})); then
  DEP=$(IFS=:; echo "${DEPENDENCIES[*]}")
  ANALYZE_ARGS+=(--dependency="afterok:${DEP}")
fi
ANALYZE_JOB=$(sbatch --parsable "${ANALYZE_ARGS[@]}" \
  --output="$RUN_DIR/logs/analyze-recovery-%j.out" --error="$RUN_DIR/logs/analyze-recovery-%j.err" \
  --export=ALL,EXP07_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e7_analyze.sbatch")
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_recovery_job_id.txt"
printf 'RUN_DIR=%s\nANALYZE_JOB_ID=%s\n' "$RUN_DIR" "$ANALYZE_JOB"
