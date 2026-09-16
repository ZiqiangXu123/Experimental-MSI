#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
RUN_BASE=${RUN_BASE:-$ROOT/runs}
RUN_DIR=${RUN_DIR:-$RUN_BASE/e6-reference-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN_DIR"/{datasets,raw_migration,raw_policy,phase1,results,logs,control,shared-work}
printf '%s\n' "$RUN_DIR" > "$ROOT/LAST_RUN_DIR"
cp "$ROOT/configs/reference.json" "$RUN_DIR/config.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" plan --config "$RUN_DIR/config.json" --output "$RUN_DIR" >/dev/null
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" write-reference-catalog --output "$RUN_DIR/component_costs.csv" --n-values 64,512,4096 >/dev/null
DATASETS=$(wc -l < "$RUN_DIR/dataset_plan.jsonl")
MIGRATION_BLOCKS=$(wc -l < "$RUN_DIR/migration_blocks.jsonl")
POLICY_BLOCKS=$(wc -l < "$RUN_DIR/policy_blocks.jsonl")
DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --array="0-$((DATASETS-1))%${DATASET_ARRAY_CONCURRENCY:-2}" --output="$RUN_DIR/logs/dataset-%A_%a.out" --error="$RUN_DIR/logs/dataset-%A_%a.err" --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_dataset.sbatch")
D=${DATASET_JOB%%;*}
MIGRATION_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$D" --array="0-$((MIGRATION_BLOCKS-1))%${MIGRATION_ARRAY_CONCURRENCY:-4}" --output="$RUN_DIR/logs/migration-%A_%a.out" --error="$RUN_DIR/logs/migration-%A_%a.err" --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_SHARED_WORK_BASE="$RUN_DIR/shared-work",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" "$ROOT/slurm/e6_migration.sbatch")
M=${MIGRATION_JOB%%;*}
PHASE1_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$M" --output="$RUN_DIR/logs/phase1-%j.out" --error="$RUN_DIR/logs/phase1-%j.err" --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_phase1_analyze.sbatch")
P1=${PHASE1_JOB%%;*}
POLICY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$P1" --array="0-$((POLICY_BLOCKS-1))%${POLICY_ARRAY_CONCURRENCY:-4}" --output="$RUN_DIR/logs/policy-%A_%a.out" --error="$RUN_DIR/logs/policy-%A_%a.err" --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" "$ROOT/slurm/e6_policy.sbatch")
P=${POLICY_JOB%%;*}
FINAL_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$P" --output="$RUN_DIR/logs/final-%j.out" --error="$RUN_DIR/logs/final-%j.err" --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_final_analyze.sbatch")
printf '%s\n' "$DATASET_JOB" > "$RUN_DIR/dataset_job_id.txt"; printf '%s\n' "$MIGRATION_JOB" > "$RUN_DIR/migration_job_id.txt"; printf '%s\n' "$PHASE1_JOB" > "$RUN_DIR/phase1_job_id.txt"; printf '%s\n' "$POLICY_JOB" > "$RUN_DIR/policy_job_id.txt"; printf '%s\n' "$FINAL_JOB" > "$RUN_DIR/final_job_id.txt"
printf 'RUN_DIR=%s\nDATASET_JOB_ID=%s\nMIGRATION_JOB_ID=%s\nPHASE1_JOB_ID=%s\nPOLICY_JOB_ID=%s\nFINAL_JOB_ID=%s\n' "$RUN_DIR" "$DATASET_JOB" "$MIGRATION_JOB" "$PHASE1_JOB" "$POLICY_JOB" "$FINAL_JOB"
