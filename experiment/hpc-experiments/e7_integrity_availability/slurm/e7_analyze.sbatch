#!/usr/bin/env bash
#SBATCH --job-name=e7-analyze
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=12G
#SBATCH --time=03:00:00
set -euo pipefail
: "${EXP07_ROOT:?}" "${RUN_DIR:?}"
rm -rf "$RUN_DIR/results"
mkdir -p "$RUN_DIR/results"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" analyze --run-dir "$RUN_DIR"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" validate --results "$RUN_DIR/results"
