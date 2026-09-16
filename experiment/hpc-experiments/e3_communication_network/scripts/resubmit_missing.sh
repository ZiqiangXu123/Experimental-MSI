#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$(cat "$ROOT/LAST_RUN_DIR" 2>/dev/null || true)}
PLAN=${2:-${RUN_DIR:+$RUN_DIR/plan.jsonl}}
if [[ -z "$RUN_DIR" || -z "$PLAN" || ! -f "$PLAN" ]]; then
  echo 'usage: resubmit_missing.sh RUN_DIR [PLAN]' >&2
  exit 64
fi
mkdir -p "$RUN_DIR"/{raw,results,logs}
missing=$("$ROOT/scripts/run_python.sh" "$ROOT/scripts/missing_indices.py" "$PLAN" "$RUN_DIR/raw")
source "$ROOT/scripts/slurm_options.sh"
TOPOLOGY=$(cat "$RUN_DIR/topology.txt" 2>/dev/null || printf '%s' 'portable-single-node-separate-processes')

if [[ -z "$missing" ]]; then
  JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
    --output="$RUN_DIR/logs/reanalyze-%j.out" \
    --error="$RUN_DIR/logs/reanalyze-%j.err" \
    "$ROOT/slurm/e3_analyze.sbatch" "$ROOT" "$RUN_DIR" "$PLAN")
  echo 'No missing or invalid blocks.'
  echo "REANALYZE_JOB_ID=$JOB"
  exit 0
fi

if [[ "$TOPOLOGY" == 'dual-node-distinct-processes' ]]; then
  CONCURRENCY=${DUAL_ARRAY_CONCURRENCY:-1}
  BLOCK_SCRIPT="$ROOT/slurm/e3_block_dual.sbatch"
  RECOVERY_OPTIONS=(--export=ALL,E3_RUN_TOPOLOGY=dual-node)
else
  CONCURRENCY=${ARRAY_CONCURRENCY:-4}
  BLOCK_SCRIPT="$ROOT/slurm/e3_block.sbatch"
  RECOVERY_OPTIONS=(--export=ALL,E3_RUN_TOPOLOGY=portable)
  if [[ "${E3_REQUEST_EXCLUSIVE:-0}" == 1 ]]; then
    RECOVERY_OPTIONS+=(--exclusive)
  fi
fi

ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${RECOVERY_OPTIONS[@]}" \
  --array="${missing}%${CONCURRENCY}" \
  --output="$RUN_DIR/logs/recovery-%A_%a.out" \
  --error="$RUN_DIR/logs/recovery-%A_%a.err" \
  "$BLOCK_SCRIPT" "$ROOT" "$RUN_DIR" "$PLAN")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/recovery-analyze-%j.out" \
  --error="$RUN_DIR/logs/recovery-analyze-%j.err" \
  "$ROOT/slurm/e3_analyze.sbatch" "$ROOT" "$RUN_DIR" "$PLAN")
printf '%s\n' "$ARRAY_JOB" > "$RUN_DIR/recovery_array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/recovery_analyze_job_id.txt"
echo "MISSING_BLOCK_INDICES=$missing"
echo "RECOVERY_TOPOLOGY=$TOPOLOGY"
echo "RECOVERY_ARRAY_JOB_ID=$ARRAY_JOB"
echo "RECOVERY_ANALYZE_JOB_ID=$ANALYZE_JOB"
