#!/usr/bin/env bash
#SBATCH --job-name=msi-e4-case-1n
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=8G
#SBATCH --time=02:00:00
set -euo pipefail
: "${E4_ROOT:?E4_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
: "${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}"
mkdir -p "$RUN_DIR/control" "$RUN_DIR/raw/queries"
CASE_JSON="$RUN_DIR/control/case-${SLURM_ARRAY_TASK_ID}.json"
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" get-case \
  --plan "$RUN_DIR/plan.jsonl" --index "$SLURM_ARRAY_TASK_ID" --output "$CASE_JSON"
if [[ "${E4_DISABLE_PINNING:-0}" == 1 ]]; then
  "$E4_ROOT/scripts/run_python.sh" - "$CASE_JSON" <<'PY'
import json, os, sys, tempfile
path=sys.argv[1]
with open(path,encoding='utf-8') as f: obj=json.load(f)
obj['pin_workers']=False
fd,tmp=tempfile.mkstemp(prefix='.case-',dir=os.path.dirname(path),text=True)
with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(obj,f,sort_keys=True,separators=(',',':'))
os.replace(tmp,path)
PY
fi
"$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" run-case \
  --case-json "$CASE_JSON" --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
echo "CASE_TASK_PASS topology=portable-single-node index=$SLURM_ARRAY_TASK_ID host=$(hostname)"
