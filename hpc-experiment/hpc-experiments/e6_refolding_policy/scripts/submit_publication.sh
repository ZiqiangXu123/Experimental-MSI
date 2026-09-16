#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"

[[ -n "${E6_COMPONENT_COST_CATALOG:-}" && -r "$E6_COMPONENT_COST_CATALOG" ]] || {
  cat >&2 <<'MSG'
ERROR: E6_COMPONENT_COST_CATALOG must point to a readable publication-eligible CSV.
Build it from the completed E1, E2, E3 results and an independently measured
availability CSV using the build-component-catalog command documented in README_CN.md.
The self-contained reference catalog is deliberately rejected for publication runs.
MSG
  exit 66
}
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" validate-component-catalog \
  --catalog "$E6_COMPONENT_COST_CATALOG" --n-values 64,512,4096,16384 --require-publication

RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e6-publication-$(date +%Y%m%d-%H%M%S)}
DATASET_ARRAY_CONCURRENCY=${DATASET_ARRAY_CONCURRENCY:-2}
MIGRATION_ARRAY_CONCURRENCY=${MIGRATION_ARRAY_CONCURRENCY:-4}
POLICY_ARRAY_CONCURRENCY=${POLICY_ARRAY_CONCURRENCY:-4}
mkdir -p "$RUN_DIR"/{datasets,raw_migration,raw_policy,phase1,results,logs,control,shared-work}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/publication.json" "$RUN_DIR/config.json"
cp "$E6_COMPONENT_COST_CATALOG" "$RUN_DIR/component_costs.csv"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR" | tee "$RUN_DIR/plan_submission.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp06_refolding_policy.pyz" "$RUN_DIR/config.json" \
  "$RUN_DIR/component_costs.csv" "$RUN_DIR/dataset_plan.jsonl" \
  "$RUN_DIR/migration_plan.jsonl" "$RUN_DIR/policy_plan.jsonl" > "$RUN_DIR/software_and_plan.sha256"

DATASETS=$(wc -l < "$RUN_DIR/dataset_plan.jsonl")
MIGRATION_BLOCKS=$(wc -l < "$RUN_DIR/migration_blocks.jsonl")
POLICY_BLOCKS=$(wc -l < "$RUN_DIR/policy_blocks.jsonl")
[[ "$DATASETS" -eq 22 && "$MIGRATION_BLOCKS" -eq 353 && "$POLICY_BLOCKS" -eq 118 ]] || {
  echo "ERROR: publication plan changed (datasets=$DATASETS migration_blocks=$MIGRATION_BLOCKS policy_blocks=$POLICY_BLOCKS)." >&2
  exit 65
}

DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --array="0-$((DATASETS-1))%${DATASET_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/dataset-%A_%a.out" --error="$RUN_DIR/logs/dataset-%A_%a.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e6_dataset.sbatch")
DATASET_BASE=${DATASET_JOB%%;*}

MIGRATION_EXTRA=()
[[ "${E6_REQUEST_EXCLUSIVE:-0}" == 1 ]] && MIGRATION_EXTRA+=(--exclusive)
MIGRATION_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${MIGRATION_EXTRA[@]}" \
  --dependency="afterok:${DATASET_BASE}" \
  --array="0-$((MIGRATION_BLOCKS-1))%${MIGRATION_ARRAY_CONCURRENCY}" \
  --output="$RUN_DIR/logs/migration-%A_%a.out" --error="$RUN_DIR/logs/migration-%A_%a.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_NODE_LOCAL_BASE="${E6_NODE_LOCAL_BASE:-}",E6_SHARED_WORK_BASE="${E6_SHARED_WORK_BASE:-$RUN_DIR/shared-work}",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
  "$ROOT/slurm/e6_migration.sbatch")
MIGRATION_BASE=${MIGRATION_JOB%%;*}

PHASE1_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${MIGRATION_BASE}" \
  --output="$RUN_DIR/logs/phase1-%j.out" --error="$RUN_DIR/logs/phase1-%j.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" \
  "$ROOT/slurm/e6_phase1_analyze.sbatch")
PHASE1_BASE=${PHASE1_JOB%%;*}

POLICY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
  --dependency="afterok:${PHASE1_BASE}" \
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

printf '%s\n' "$DATASET_JOB" > "$RUN_DIR/dataset_job_id.txt"
printf '%s\n' "$MIGRATION_JOB" > "$RUN_DIR/migration_job_id.txt"
printf '%s\n' "$PHASE1_JOB" > "$RUN_DIR/phase1_job_id.txt"
printf '%s\n' "$POLICY_JOB" > "$RUN_DIR/policy_job_id.txt"
printf '%s\n' "$FINAL_JOB" > "$RUN_DIR/final_job_id.txt"
printf 'RUN_DIR=%s\nDATASET_JOB_ID=%s\nMIGRATION_JOB_ID=%s\nPHASE1_JOB_ID=%s\nPOLICY_JOB_ID=%s\nFINAL_JOB_ID=%s\n' \
  "$RUN_DIR" "$DATASET_JOB" "$MIGRATION_JOB" "$PHASE1_JOB" "$POLICY_JOB" "$FINAL_JOB"
