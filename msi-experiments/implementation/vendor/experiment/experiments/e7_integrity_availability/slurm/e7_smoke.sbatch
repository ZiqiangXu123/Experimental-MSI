#!/usr/bin/env bash
#SBATCH --job-name=e7-smoke
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:45:00
set -euo pipefail
: "${EXP07_ROOT:?}" "${RUN_DIR:?}"
mkdir -p "$RUN_DIR/raw_e7" "$RUN_DIR/raw_e8" "$RUN_DIR/results"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" plan \
  --config "$RUN_DIR/config.json" --output "$RUN_DIR"
E7_BLOCKS=$(wc -l < "$RUN_DIR/e7_blocks.jsonl")
E8_BLOCKS=$(wc -l < "$RUN_DIR/e8_blocks.jsonl")
for ((i=0; i<E7_BLOCKS; i++)); do
  "$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" run-e7-block \
    --plan-dir "$RUN_DIR" --index "$i" --raw "$RUN_DIR/raw_e7"
done
for ((i=0; i<E8_BLOCKS; i++)); do
  "$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" run-e8-block \
    --plan-dir "$RUN_DIR" --index "$i" --raw "$RUN_DIR/raw_e8"
done
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" analyze --run-dir "$RUN_DIR"
"$EXP07_ROOT/scripts/run_python.sh" "$EXP07_ROOT/exp07_security_availability.pyz" validate --results "$RUN_DIR/results"
