#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_DIR=${1:-$(mktemp -d "${TMPDIR:-/tmp}/e6-local-smoke.XXXXXX")}
mkdir -p "$RUN_DIR"/{datasets,raw_migration,raw_policy,phase1,results,work}
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" plan --config "$ROOT/configs/smoke.json" --output "$RUN_DIR"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" write-reference-catalog --output "$RUN_DIR/component_costs.csv" --n-values 8,65
while IFS= read -r index; do
  "$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" build-dataset --plan "$RUN_DIR/dataset_plan.jsonl" --datasets "$RUN_DIR/datasets" --index "$index" >/dev/null
done < <(seq 0 $(( $(wc -l < "$RUN_DIR/dataset_plan.jsonl") - 1 )))
while IFS= read -r index; do
  "$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" run-migration-block --plan "$RUN_DIR/migration_plan.jsonl" --blocks "$RUN_DIR/migration_blocks.jsonl" --block-index "$index" --datasets "$RUN_DIR/datasets" --raw "$RUN_DIR/raw_migration" --work-root "$RUN_DIR/work" >/dev/null
done < <(seq 0 $(( $(wc -l < "$RUN_DIR/migration_blocks.jsonl") - 1 )))
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" analyze-migration --plan "$RUN_DIR/migration_plan.jsonl" --raw "$RUN_DIR/raw_migration" --output "$RUN_DIR/phase1" >/dev/null
while IFS= read -r index; do
  "$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" run-policy-block --plan "$RUN_DIR/policy_plan.jsonl" --blocks "$RUN_DIR/policy_blocks.jsonl" --block-index "$index" --component-costs "$RUN_DIR/component_costs.csv" --migration-catalog "$RUN_DIR/phase1/migration_cost_catalog.csv" --raw "$RUN_DIR/raw_policy" >/dev/null
done < <(seq 0 $(( $(wc -l < "$RUN_DIR/policy_blocks.jsonl") - 1 )))
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" analyze --config "$RUN_DIR/resolved_config.json" --migration-plan "$RUN_DIR/migration_plan.jsonl" --migration-raw "$RUN_DIR/raw_migration" --policy-plan "$RUN_DIR/policy_plan.jsonl" --policy-raw "$RUN_DIR/raw_policy" --component-costs "$RUN_DIR/component_costs.csv" --output "$RUN_DIR/results"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" validate --results "$RUN_DIR/results"
printf 'RUN_DIR=%s\nLOCAL_SMOKE=PASS\n' "$RUN_DIR"
