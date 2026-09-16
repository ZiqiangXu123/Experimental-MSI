#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e2-publication-$(date +%Y%m%d-%H%M%S)}
ARRAY_CONCURRENCY=${ARRAY_CONCURRENCY:-4}
mkdir -p "$RUN_DIR"/{raw,results,logs}

"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" make-plan \
  --config "$ROOT/configs/publication.json" \
  --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
BLOCKS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind blocks)
CASES=$("$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" count-plan \
  --plan "$RUN_DIR/plan.jsonl" --kind cases)
[[ "$BLOCKS" == 290 && "$CASES" == 1330 ]] || {
  echo "ERROR: publication plan changed unexpectedly (blocks=$BLOCKS cases=$CASES)" >&2
  exit 65
}
LAST=$((BLOCKS - 1))
sha256sum "$ROOT/exp02_query.pyz" "$ROOT/configs/publication.json" > "$RUN_DIR/software.sha256"
cp "$ROOT/configs/publication.json" "$RUN_DIR/publication_config.json"

source "$ROOT/scripts/slurm_options.sh"
BENCH_OPTIONS=()
if [[ "${E2_REQUEST_EXCLUSIVE:-0}" == 1 ]]; then
  BENCH_OPTIONS+=(--exclusive --export=ALL,E2_BENCHMARK_EXCLUSIVE=1)
else
  BENCH_OPTIONS+=(--export=ALL,E2_BENCHMARK_EXCLUSIVE=0)
fi
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${BENCH_OPTIONS[@]}" \
  --array="0-${LAST}%${ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/block-%A_%a.out" \
  --error="$RUN_DIR/logs/block-%A_%a.err" \
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
echo "BLOCKS=$BLOCKS"
echo "INDEPENDENT_PROCESS_CASES=$CASES"
echo "ARRAY_JOB_ID=$ARRAY_JOB"
echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
echo "EXCLUSIVE_REQUESTED=${E2_REQUEST_EXCLUSIVE:-0}"
echo "Monitor: squeue -j ${ARRAY_ID},${ANALYZE_JOB%%;*}"
