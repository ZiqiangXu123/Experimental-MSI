#!/usr/bin/env bash
#SBATCH --job-name=e6-smoke
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=6G
#SBATCH --time=03:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}"
mkdir -p "$RUN_DIR"/{datasets,raw_migration,raw_policy,phase1,results,work,shared-work}
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" write-reference-catalog \
  --output "$RUN_DIR/component_costs.csv" --n-values 8,65
DATASETS=$(wc -l < "$RUN_DIR/dataset_plan.jsonl")
for ((i=0; i<DATASETS; i++)); do
  "$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" build-dataset \
    --plan "$RUN_DIR/dataset_plan.jsonl" --datasets "$RUN_DIR/datasets" --index "$i" >/dev/null
done
MIGRATION_BLOCKS=$(wc -l < "$RUN_DIR/migration_blocks.jsonl")
for ((i=0; i<MIGRATION_BLOCKS; i++)); do
  "$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" run-migration-block \
    --plan "$RUN_DIR/migration_plan.jsonl" --blocks "$RUN_DIR/migration_blocks.jsonl" \
    --block-index "$i" --datasets "$RUN_DIR/datasets" --raw "$RUN_DIR/raw_migration" \
    --work-root "${SLURM_TMPDIR:-$RUN_DIR/work}" >/dev/null
done
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" analyze-migration \
  --plan "$RUN_DIR/migration_plan.jsonl" --raw "$RUN_DIR/raw_migration" --output "$RUN_DIR/phase1" >/dev/null
POLICY_BLOCKS=$(wc -l < "$RUN_DIR/policy_blocks.jsonl")
for ((i=0; i<POLICY_BLOCKS; i++)); do
  "$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" run-policy-block \
    --plan "$RUN_DIR/policy_plan.jsonl" --blocks "$RUN_DIR/policy_blocks.jsonl" \
    --block-index "$i" --component-costs "$RUN_DIR/component_costs.csv" \
    --migration-catalog "$RUN_DIR/phase1/migration_cost_catalog.csv" \
    --raw "$RUN_DIR/raw_policy" >/dev/null
done
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" analyze \
  --config "$RUN_DIR/resolved_config.json" --migration-plan "$RUN_DIR/migration_plan.jsonl" \
  --migration-raw "$RUN_DIR/raw_migration" --policy-plan "$RUN_DIR/policy_plan.jsonl" \
  --policy-raw "$RUN_DIR/raw_policy" --component-costs "$RUN_DIR/component_costs.csv" \
  --output "$RUN_DIR/results"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" validate --results "$RUN_DIR/results"
