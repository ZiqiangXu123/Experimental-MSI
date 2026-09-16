#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e3-publication-dual-$(date +%Y%m%d-%H%M%S)}
DUAL_ARRAY_CONCURRENCY=${DUAL_ARRAY_CONCURRENCY:-1}
mkdir -p "$RUN_DIR"/{raw,results,logs,control}
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" make-plan \
  --config "$ROOT/configs/publication.json" --output "$RUN_DIR/plan.jsonl" | tee "$RUN_DIR/plan_summary.json"
BLOCKS=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$RUN_DIR/plan.jsonl" --kind blocks)
CASES=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$RUN_DIR/plan.jsonl" --kind cases)
[[ "$BLOCKS" == 156 && "$CASES" == 624 ]] || { echo 'ERROR: publication plan changed' >&2; exit 65; }
LAST=$((BLOCKS - 1))
sha256sum "$ROOT/exp03_network.pyz" "$ROOT/configs/publication.json" > "$RUN_DIR/software.sha256"
cp "$ROOT/configs/publication.json" "$RUN_DIR/publication_config.json"
printf '%s\n' 'dual-node-distinct-processes' > "$RUN_DIR/topology.txt"
source "$ROOT/scripts/slurm_options.sh"
ARRAY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --export=ALL,E3_RUN_TOPOLOGY=dual-node \
  --array="0-${LAST}%${DUAL_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/dual-%A_%a.out" --error="$RUN_DIR/logs/dual-%A_%a.err" \
  "$ROOT/slurm/e3_block_dual.sbatch" "$ROOT" "$RUN_DIR" "$RUN_DIR/plan.jsonl")
ARRAY_ID=${ARRAY_JOB%%;*}
ANALYZE_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${ARRAY_ID}" \
  --output="$RUN_DIR/logs/analyze-%j.out" --error="$RUN_DIR/logs/analyze-%j.err" \
  "$ROOT/slurm/e3_analyze.sbatch" "$ROOT" "$RUN_DIR" "$RUN_DIR/plan.jsonl")
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
printf '%s\n' "$ARRAY_JOB" > "$RUN_DIR/array_job_id.txt"
printf '%s\n' "$ANALYZE_JOB" > "$RUN_DIR/analyze_job_id.txt"
echo "RUN_DIR=$RUN_DIR"
echo "BLOCKS=$BLOCKS CASES=$CASES"
echo "ARRAY_JOB_ID=$ARRAY_JOB"
echo "ANALYZE_JOB_ID=$ANALYZE_JOB"
echo 'TOPOLOGY=dual-node-distinct-processes'
