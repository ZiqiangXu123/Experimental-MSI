#!/usr/bin/env bash
#SBATCH --job-name=msi-e5-case
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=01:00:00
set -euo pipefail
: "${E5_ROOT:?E5_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
: "${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}"
mkdir -p "$RUN_DIR/control" "$RUN_DIR/raw"
CASE_JSON="$RUN_DIR/control/case-${SLURM_ARRAY_TASK_ID}.json"
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" get-case \
  --plan "$RUN_DIR/plan.jsonl" --index "$SLURM_ARRAY_TASK_ID" --output "$CASE_JSON"
if [[ "${E5_DISABLE_PINNING:-0}" == 1 ]]; then
  "$E5_ROOT/scripts/run_python.sh" - "$CASE_JSON" <<'PY'
import json, os, sys, tempfile
path=sys.argv[1]
with open(path,encoding='utf-8') as handle:
    obj=json.load(handle)
obj['disable_pinning']=True
fd,tmp=tempfile.mkstemp(prefix='.case-',dir=os.path.dirname(path),text=True)
with os.fdopen(fd,'w',encoding='utf-8') as handle:
    json.dump(obj,handle,sort_keys=True,separators=(',',':'))
os.replace(tmp,path)
PY
fi
"$E5_ROOT/scripts/run_python.sh" "$E5_ROOT/exp05_anchor.pyz" run-case \
  --case-json "$CASE_JSON" --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw"
echo "CASE_PASS index=$SLURM_ARRAY_TASK_ID host=$(hostname)"
