#!/usr/bin/env bash
#SBATCH --job-name=msi-supplement
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --output=msi-%j.out

set -euo pipefail
MSI_BATCH_ROOT="${MSI_SUITE_DIR:-${SLURM_SUBMIT_DIR:-$PWD}}"
if [[ ! -f "$MSI_BATCH_ROOT/run.sh" ]]; then
    echo "Submit from the suite root or set MSI_SUITE_DIR to its absolute path." >&2
    exit 2
fi
cd "$MSI_BATCH_ROOT"
OUT="${OUT:-results/hpc-${SLURM_JOB_ID:-local}-$(date -u +%Y%m%dT%H%M%SZ)}"
PROFILE="${PROFILE:-smoke}"
MSI_BATCH_ARGS=(run --role hpc --profile "$PROFILE" --out "$OUT")
if [[ -n "${DATASET:-}" ]]; then
    MSI_BATCH_ARGS+=(--dataset "$DATASET")
fi
exec bash ./run.sh "${MSI_BATCH_ARGS[@]}"
