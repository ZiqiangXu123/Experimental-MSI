#!/usr/bin/env bash
#SBATCH --job-name=msi-e2-analyze
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4096
#SBATCH --time=01:00:00

set -euo pipefail
ROOT=${1:?usage: e2_analyze.sbatch ROOT RUN_DIR PLAN}
RUN_DIR=${2:?usage: e2_analyze.sbatch ROOT RUN_DIR PLAN}
PLAN=${3:?usage: e2_analyze.sbatch ROOT RUN_DIR PLAN}
mkdir -p "$RUN_DIR/results"
echo "START $(date -Is 2>/dev/null || date)"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" analyze \
  --raw "$RUN_DIR/raw" --output "$RUN_DIR/results" --plan "$PLAN"
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" validate --results "$RUN_DIR/results"
(cd "$RUN_DIR/results" && sha256sum -c manifest.sha256)
echo "END $(date -Is 2>/dev/null || date)"
