#!/usr/bin/env bash
#SBATCH --job-name=e6-analyze
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=12G
#SBATCH --time=03:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}"
rm -rf "$RUN_DIR/results"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" analyze \
  --config "$RUN_DIR/resolved_config.json" \
  --migration-plan "$RUN_DIR/migration_plan.jsonl" \
  --migration-raw "$RUN_DIR/raw_migration" \
  --policy-plan "$RUN_DIR/policy_plan.jsonl" \
  --policy-raw "$RUN_DIR/raw_policy" \
  --component-costs "$RUN_DIR/component_costs.csv" \
  --output "$RUN_DIR/results"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" validate --results "$RUN_DIR/results"
