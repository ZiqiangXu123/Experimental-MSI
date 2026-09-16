#!/usr/bin/env bash
#SBATCH --job-name=msi-e2-smoke
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2048
#SBATCH --time=00:45:00

set -euo pipefail
ROOT=${1:?usage: e2_smoke.sbatch ROOT RUN_DIR}
RUN_DIR=${2:?usage: e2_smoke.sbatch ROOT RUN_DIR}
mkdir -p "$RUN_DIR"/{raw,results,logs}
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

echo "START $(date -Is 2>/dev/null || date)"
echo "HOST $(hostname -f 2>/dev/null || hostname)"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" info > "$RUN_DIR/environment_info.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" make-plan \
  --config "$ROOT/configs/smoke.json" --output "$RUN_DIR/plan.jsonl" \
  | tee "$RUN_DIR/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" run-plan \
  --plan "$RUN_DIR/plan.jsonl" --output "$RUN_DIR/raw" \
  --launcher "$ROOT/exp02_query.pyz"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" analyze \
  --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$RUN_DIR/plan.jsonl"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" validate --results "$RUN_DIR/results"
(cd "$RUN_DIR/results" && sha256sum -c manifest.sha256)
echo "END $(date -Is 2>/dev/null || date)"
