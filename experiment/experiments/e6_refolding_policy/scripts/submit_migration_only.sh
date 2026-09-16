#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"

RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e6-migration-publication-$(date +%Y%m%d-%H%M%S)}
DATASET_ARRAY_CONCURRENCY=${DATASET_ARRAY_CONCURRENCY:-2}
MIGRATION_ARRAY_CONCURRENCY=${MIGRATION_ARRAY_CONCURRENCY:-4}
mkdir -p "$RUN_DIR"/{datasets,raw_migration,raw_policy,phase1,results,logs,control,shared-work}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/publication.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR" | tee "$RUN_DIR/plan_submission.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" info > "$RUN_DIR/submission_environment.json"
sha256sum "$ROOT/exp06_refolding_policy.pyz" "$RUN_DIR/config.json" \
  "$RUN_DIR/dataset_plan.jsonl" "$RUN_DIR/migration_plan.jsonl" > "$RUN_DIR/software_and_migration_plan.sha256"

DATASETS=$(wc -l < "$RUN_DIR/dataset_plan.jsonl")
MIGRATION_BLOCKS=$(wc -l < "$RUN_DIR/migration_blocks.jsonl")
[[ "$DATASETS" -eq 22 && "$MIGRATION_BLOCKS" -eq 353 ]] || {
  echo "ERROR: publication migration plan changed (datasets=$DATASETS migration_blocks=$MIGRATION_BLOCKS)." >&2
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

printf '%s\n' "$DATASET_JOB" > "$RUN_DIR/dataset_job_id.txt"
printf '%s\n' "$MIGRATION_JOB" > "$RUN_DIR/migration_job_id.txt"
printf '%s\n' "$PHASE1_JOB" > "$RUN_DIR/phase1_job_id.txt"
printf '%s\n' 'migration-only; append E9 later with scripts/submit_policy_only.sh' > "$RUN_DIR/workflow_stage.txt"
printf 'RUN_DIR=%s\nDATASET_JOB_ID=%s\nMIGRATION_JOB_ID=%s\nPHASE1_JOB_ID=%s\n' \
  "$RUN_DIR" "$DATASET_JOB" "$MIGRATION_JOB" "$PHASE1_JOB"
