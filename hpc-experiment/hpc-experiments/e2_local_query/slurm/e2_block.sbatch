#!/usr/bin/env bash
#SBATCH --job-name=msi-e2-block
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4096
#SBATCH --time=04:00:00

set -euo pipefail
ulimit -c 0
ROOT=${1:?usage: e2_block.sbatch ROOT RUN_DIR PLAN}
RUN_DIR=${2:?usage: e2_block.sbatch ROOT RUN_DIR PLAN}
PLAN=${3:?usage: e2_block.sbatch ROOT RUN_DIR PLAN}
INDEX=${SLURM_ARRAY_TASK_ID:?this must run as a Slurm array task}
mkdir -p "$RUN_DIR/raw"
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

echo "START $(date -Is 2>/dev/null || date)"
echo "HOST $(hostname -f 2>/dev/null || hostname)"
echo "JOB ${SLURM_JOB_ID:-none} ARRAY ${SLURM_ARRAY_JOB_ID:-none} TASK $INDEX"
echo "CPUS_ALLOWED $(grep Cpus_allowed_list /proc/self/status 2>/dev/null || true)"
echo "BENCHMARK_EXCLUSIVE ${E2_BENCHMARK_EXCLUSIVE:-0}"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" run-block \
  --plan "$PLAN" --index "$INDEX" --output "$RUN_DIR/raw" \
  --launcher "$ROOT/exp02_query.pyz"
echo "END $(date -Is 2>/dev/null || date)"
