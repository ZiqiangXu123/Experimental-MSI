#!/usr/bin/env bash
#SBATCH --job-name=e6-phase1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=02:00:00
set -euo pipefail
: "${E6_ROOT:?}" "${RUN_DIR:?}"
rm -rf "$RUN_DIR/phase1"
"$E6_ROOT/scripts/run_python.sh" "$E6_ROOT/exp06_refolding_policy.pyz" analyze-migration \
  --plan "$RUN_DIR/migration_plan.jsonl" \
  --raw "$RUN_DIR/raw_migration" \
  --output "$RUN_DIR/phase1" \
  --preferred-backend node_local
