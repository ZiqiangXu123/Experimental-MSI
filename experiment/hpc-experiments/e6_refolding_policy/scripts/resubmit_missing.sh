#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: resubmit_missing.sh RUN_DIR}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"
source "$ROOT/scripts/slurm_options.sh"
for required in resolved_config.json dataset_plan.jsonl migration_plan.jsonl migration_blocks.jsonl policy_plan.jsonl policy_blocks.jsonl component_costs.csv; do
  [[ -f "$RUN_DIR/$required" ]] || { echo "ERROR: $RUN_DIR/$required is missing." >&2; exit 66; }
done

MISSING_MIGRATION=$("$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" missing-migration-blocks \
  --plan "$RUN_DIR/migration_plan.jsonl" --blocks "$RUN_DIR/migration_blocks.jsonl" --raw "$RUN_DIR/raw_migration")
MISSING_POLICY=$("$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" missing-policy-blocks \
  --plan "$RUN_DIR/policy_plan.jsonl" --blocks "$RUN_DIR/policy_blocks.jsonl" --raw "$RUN_DIR/raw_policy")

DEPENDENCY=''
if [[ -n "$MISSING_MIGRATION" ]]; then
  echo "Missing/corrupt migration blocks: $MISSING_MIGRATION"
  rm -rf "$RUN_DIR/raw_policy" "$RUN_DIR/phase1" "$RUN_DIR/results"
  mkdir -p "$RUN_DIR/raw_policy" "$RUN_DIR/phase1" "$RUN_DIR/results"
  DATASETS=$(wc -l < "$RUN_DIR/dataset_plan.jsonl")
  DATASET_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
    --array="0-$((DATASETS-1))%${DATASET_ARRAY_CONCURRENCY:-2}" \
    --output="$RUN_DIR/logs/recovery-dataset-%A_%a.out" --error="$RUN_DIR/logs/recovery-dataset-%A_%a.err" \
    --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_dataset.sbatch")
  D=${DATASET_JOB%%;*}
  EXTRA=(); [[ "${E6_REQUEST_EXCLUSIVE:-0}" == 1 ]] && EXTRA+=(--exclusive)
  MIGRATION_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" "${EXTRA[@]}" \
    --dependency="afterok:$D" --array="${MISSING_MIGRATION}%${MIGRATION_ARRAY_CONCURRENCY:-4}" \
    --output="$RUN_DIR/logs/recovery-migration-%A_%a.out" --error="$RUN_DIR/logs/recovery-migration-%A_%a.err" \
    --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_SHARED_WORK_BASE="${E6_SHARED_WORK_BASE:-$RUN_DIR/shared-work}",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
    "$ROOT/slurm/e6_migration.sbatch")
  M=${MIGRATION_JOB%%;*}
  PHASE1_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$M" \
    --output="$RUN_DIR/logs/recovery-phase1-%j.out" --error="$RUN_DIR/logs/recovery-phase1-%j.err" \
    --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_phase1_analyze.sbatch")
  P1=${PHASE1_JOB%%;*}
  POLICY_BLOCKS=$(wc -l < "$RUN_DIR/policy_blocks.jsonl")
  POLICY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$P1" \
    --array="0-$((POLICY_BLOCKS-1))%${POLICY_ARRAY_CONCURRENCY:-4}" \
    --output="$RUN_DIR/logs/recovery-policy-%A_%a.out" --error="$RUN_DIR/logs/recovery-policy-%A_%a.err" \
    --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
    "$ROOT/slurm/e6_policy.sbatch")
  P=${POLICY_JOB%%;*}
  DEPENDENCY="afterok:$P"
  printf '%s\n' "$DATASET_JOB" >> "$RUN_DIR/recovery_dataset_job_ids.txt"
  printf '%s\n' "$MIGRATION_JOB" >> "$RUN_DIR/recovery_migration_job_ids.txt"
  printf '%s\n' "$PHASE1_JOB" >> "$RUN_DIR/recovery_phase1_job_ids.txt"
  printf '%s\n' "$POLICY_JOB" >> "$RUN_DIR/recovery_policy_job_ids.txt"
else
  echo 'No missing migration blocks; regenerating the phase-1 catalog for consistency.'
  PHASE1_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" \
    --output="$RUN_DIR/logs/recovery-phase1-%j.out" --error="$RUN_DIR/logs/recovery-phase1-%j.err" \
    --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_phase1_analyze.sbatch")
  P1=${PHASE1_JOB%%;*}
  printf '%s\n' "$PHASE1_JOB" >> "$RUN_DIR/recovery_phase1_job_ids.txt"
  if [[ -n "$MISSING_POLICY" ]]; then
    echo "Missing/corrupt policy blocks: $MISSING_POLICY"
    POLICY_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="afterok:$P1" \
      --array="${MISSING_POLICY}%${POLICY_ARRAY_CONCURRENCY:-4}" \
      --output="$RUN_DIR/logs/recovery-policy-%A_%a.out" --error="$RUN_DIR/logs/recovery-policy-%A_%a.err" \
      --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR",E6_DISABLE_PINNING="${E6_DISABLE_PINNING:-0}" \
      "$ROOT/slurm/e6_policy.sbatch")
    P=${POLICY_JOB%%;*}
    DEPENDENCY="afterok:$P"
    printf '%s\n' "$POLICY_JOB" >> "$RUN_DIR/recovery_policy_job_ids.txt"
  else
    echo 'No missing policy blocks.'
    DEPENDENCY="afterok:$P1"
  fi
fi

FINAL_JOB=$(sbatch --parsable "${SBATCH_SITE_OPTIONS[@]}" --dependency="$DEPENDENCY" \
  --output="$RUN_DIR/logs/recovery-final-%j.out" --error="$RUN_DIR/logs/recovery-final-%j.err" \
  --export=ALL,E6_ROOT="$ROOT",RUN_DIR="$RUN_DIR" "$ROOT/slurm/e6_final_analyze.sbatch")
printf '%s\n' "$FINAL_JOB" >> "$RUN_DIR/recovery_final_job_ids.txt"
printf 'MISSING_MIGRATION_BLOCKS=%s\nMISSING_POLICY_BLOCKS=%s\nFINAL_JOB_ID=%s\n' \
  "$MISSING_MIGRATION" "$MISSING_POLICY" "$FINAL_JOB"
