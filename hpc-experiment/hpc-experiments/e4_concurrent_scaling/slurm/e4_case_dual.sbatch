#!/usr/bin/env bash
#SBATCH --job-name=msi-e4-case-2n
#SBATCH --nodes=2
#SBATCH --ntasks=2
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=8G
#SBATCH --time=02:00:00
set -euo pipefail
: "${E4_ROOT:?E4_ROOT is required}"
: "${RUN_DIR:?RUN_DIR is required}"
: "${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}"
mkdir -p "$RUN_DIR/control" "$RUN_DIR/raw/queries"
mapfile -t NODES < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
[[ ${#NODES[@]} -ge 2 ]] || { echo 'ERROR: dual-node task received fewer than two nodes.' >&2; exit 70; }
SERVICE_NODE=${NODES[0]}
VERIFIER_NODE=${NODES[1]}
CASE_JSON="$RUN_DIR/control/case-${SLURM_ARRAY_TASK_ID}.json"
READY_JSON="$RUN_DIR/control/service-ready-${SLURM_ARRAY_TASK_ID}.json"
FINAL_JSON="$RUN_DIR/control/service-final-${SLURM_ARRAY_TASK_ID}.json"
rm -f "$READY_JSON" "$FINAL_JSON"
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
cleanup() {
  if [[ -n "${SERVICE_STEP_PID:-}" ]]; then
    kill "$SERVICE_STEP_PID" 2>/dev/null || true
    wait "$SERVICE_STEP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
srun --overlap --exact --nodes=1 --ntasks=1 --nodelist="$SERVICE_NODE" \
  --cpus-per-task="${SLURM_CPUS_PER_TASK:-8}" \
  "$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" service \
    --case-json "$CASE_JSON" --bind-host 0.0.0.0 --advertise-host "$SERVICE_NODE" \
    --ready "$READY_JSON" --final-stats "$FINAL_JSON" &
SERVICE_STEP_PID=$!
deadline=$((SECONDS+120))
while [[ ! -s "$READY_JSON" ]]; do
  kill -0 "$SERVICE_STEP_PID" 2>/dev/null || { echo 'ERROR: retrieval service exited before readiness.' >&2; wait "$SERVICE_STEP_PID"; exit 71; }
  (( SECONDS < deadline )) || { echo 'ERROR: retrieval service readiness timeout.' >&2; exit 72; }
  sleep 0.2
done
srun --overlap --exact --nodes=1 --ntasks=1 --nodelist="$VERIFIER_NODE" \
  --cpus-per-task="${SLURM_CPUS_PER_TASK:-8}" \
  "$E4_ROOT/scripts/run_python.sh" "$E4_ROOT/exp04_scaling.pyz" run-case \
    --case-json "$CASE_JSON" --dataset-dir "$RUN_DIR/datasets" --raw-dir "$RUN_DIR/raw" \
    --external-ready "$READY_JSON"
wait "$SERVICE_STEP_PID"
SERVICE_STEP_PID=''
echo "CASE_TASK_PASS topology=dual-node service=$SERVICE_NODE verifier=$VERIFIER_NODE index=$SLURM_ARRAY_TASK_ID"
