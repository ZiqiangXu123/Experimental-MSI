#!/usr/bin/env bash
#SBATCH --job-name=e6-policy
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=10G
#SBATCH --time=06:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}" "${SLURM_ARRAY_TASK_ID:?}"
mkdir -p "$RUN_DIR/raw_policy"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" run-policy-block \
  --plan "$RUN_DIR/policy_plan.jsonl" \
  --blocks "$RUN_DIR/policy_blocks.jsonl" \
  --block-index "$SLURM_ARRAY_TASK_ID" \
  --component-costs "$RUN_DIR/component_costs.csv" \
  --migration-catalog "$RUN_DIR/phase1/migration_cost_catalog.csv" \
  --raw "$RUN_DIR/raw_policy"
