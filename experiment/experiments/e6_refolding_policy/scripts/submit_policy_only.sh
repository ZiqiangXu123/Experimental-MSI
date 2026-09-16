#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: submit_policy_only.sh RUN_DIR [PUBLICATION_COMPONENT_CATALOG]}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
CATALOG=${2:-${E6_COMPONENT_COST_CATALOG:-}}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"

for required in resolved_config.json migration_plan.jsonl policy_plan.jsonl policy_blocks.jsonl; do
  [[ -f "$RUN_DIR/$required" ]] || { echo "ERROR: $RUN_DIR/$required is missing." >&2; exit 66; }
done
[[ -n "$CATALOG" && -r "$CATALOG" ]] || {
  echo 'ERROR: provide a readable publication-eligible component catalog as argument 2 or E6_COMPONENT_COST_CATALOG.' >&2
  exit 66
}
[[ -r "$RUN_DIR/phase1/migration_cost_catalog.csv" ]] || {
  echo 'ERROR: phase-1 migration analysis is incomplete; migration_cost_catalog.csv is missing.' >&2
  exit 66
}

"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" inspect-migration \
  --plan "$RUN_DIR/migration_plan.jsonl" --raw "$RUN_DIR/raw_migration"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" validate-component-catalog \
  --catalog "$CATALOG" --n-values 64,512,4096,16384 --require-publication

cp "$CATALOG" "$RUN_DIR/component_costs.csv.tmp"
mv -f "$RUN_DIR/component_costs.csv.tmp" "$RUN_DIR/component_costs.csv"
rm -rf "$RUN_DIR/raw_policy" "$RUN_DIR/results"
mkdir -p "$RUN_DIR/raw_policy" "$RUN_DIR/results" "$RUN_DIR/logs"
sha256sum "$RUN_DIR/component_costs.csv" "$RUN_DIR/phase1/migration_cost_catalog.csv" \
  > "$RUN_DIR/policy_input_catalogs.sha256"

POLICY_BLOCKS=$(wc -l < "$RUN_DIR/policy_blocks.jsonl")
[[ "$POLICY_BLOCKS" -eq 118 ]] || {
  echo "ERROR: publication policy plan changed (policy_blocks=$POLICY_BLOCKS)." >&2
  exit 65
}
POLICY_ARRAY_CONCURRENCY=${POLICY_ARRAY_CONCURRENCY:-4}
POLICY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-$((POLICY_BLOCKS-1))%${POLICY_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/policy-%A_%a.out" --error="$RUN_DIR/logs/policy-%A_%a.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
  "$ROOT/slurm/e6_policy.sbatch")
POLICY_BASE=${POLICY_JOB%%;*}

FINAL_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${POLICY_BASE}" \
  --output="$RUN_DIR/logs/final-%j.out" --error="$RUN_DIR/logs/final-%j.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e6_final_analyze.sbatch")

printf '%s\n' "$POLICY_JOB" > "$RUN_DIR/policy_job_id.txt"
printf '%s\n' "$FINAL_JOB" > "$RUN_DIR/final_job_id.txt"
printf '%s\n' 'complete E6+E9 publication workflow' > "$RUN_DIR/workflow_stage.txt"
printf 'RUN_DIR=%s\nPOLICY_JOB_ID=%s\nFINAL_JOB_ID=%s\n' "$RUN_DIR" "$POLICY_JOB" "$FINAL_JOB"
