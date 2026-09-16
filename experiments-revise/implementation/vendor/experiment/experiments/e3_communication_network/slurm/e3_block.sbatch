#!/usr/bin/env bash
#SBATCH --job-name=msi-e3-block
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8192
#SBATCH --time=02:00:00

set -euo pipefail
ulimit -c 0
ROOT=${1:?usage: e3_block.sbatch ROOT RUN_DIR PLAN}
RUN_DIR=${2:?usage: e3_block.sbatch ROOT RUN_DIR PLAN}
PLAN=${3:?usage: e3_block.sbatch ROOT RUN_DIR PLAN}
INDEX=${SLURM_ARRAY_TASK_ID:?this must run as a Slurm array task}
mkdir -p "$RUN_DIR/raw"
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export E3_DISABLE_PINNING=${E3_DISABLE_PINNING:-0}

echo "START $(date -Is 2>/dev/null || date)"
echo "HOST $(hostname -f 2>/dev/null || hostname)"
echo "JOB ${SLURM_JOB_ID:-none} ARRAY ${SLURM_ARRAY_JOB_ID:-none} TASK $INDEX"
echo "TOPOLOGY portable-single-node-separate-processes"
echo "CPUS_ALLOWED $(grep Cpus_allowed_list /proc/self/status 2>/dev/null || true)"
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" run-block \
  --plan "$PLAN" --index "$INDEX" --output "$RUN_DIR/raw" \
  --launcher "$ROOT/exp03_network.pyz"
echo "END $(date -Is 2>/dev/null || date)"
