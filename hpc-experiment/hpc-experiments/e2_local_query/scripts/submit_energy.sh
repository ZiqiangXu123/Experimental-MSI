#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e2-energy-$(date +%Y%m%d-%H%M%S)}
ENERGY_ARRAY_CONCURRENCY=${ENERGY_ARRAY_CONCURRENCY:-1}
mkdir -p "$RUN_DIR"/{raw,results,logs}
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" make-plan \
  --config "$ROOT/configs/energy.json" --output "$RUN_DIR/plan.jsonl" \
  | tee "$RUN_DIR/plan_summary.json"
BLOCKS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind blocks)
LAST=$((BLOCKS - 1))
source "$ROOT/scripts/slurm_options.sh"
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --exclusive --export=ALL,E2_ENERGY_EXCLUSIVE=1,E2_BENCHMARK_EXCLUSIVE=1 \
  --array="0-${LAST}%${ENERGY_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/energy-%A_%a.out" \
  --error="$RUN_DIR/logs/energy-%A_%a.err" \
  "$ROOT/slurm/e2_block.sbatch" "$ROOT" "$RUN_DIR" "$RUN_DIR/plan.jsonl")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/analyze-%j.out" \
  --error="$RUN_DIR/logs/analyze-%j.err" \
  "$ROOT/slurm/e2_analyze.sbatch" "$ROOT" "$RUN_DIR" "$RUN_DIR/plan.jsonl")
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
printf '%s\n' "$ARRAY_JOB" > "$RUN_DIR/array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_job_id.txt"
echo "RUN_DIR=$RUN_DIR"
echo "ENERGY_ARRAY_JOB_ID=$ARRAY_JOB"
echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
echo 'WARNING: server-package energy is supplementary and does not replace an edge-device energy experiment.'
