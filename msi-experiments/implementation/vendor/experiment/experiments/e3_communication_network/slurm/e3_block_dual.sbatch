#!/usr/bin/env bash
#SBATCH --job-name=msi-e3-dual
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=2
#SBATCH --cpus-per-task=1
#SBATCH --mem=8192
#SBATCH --time=02:00:00

set -euo pipefail
ulimit -c 0
ROOT=${1:?usage: e3_block_dual.sbatch ROOT RUN_DIR PLAN}
RUN_DIR=${2:?usage: e3_block_dual.sbatch ROOT RUN_DIR PLAN}
PLAN=${3:?usage: e3_block_dual.sbatch ROOT RUN_DIR PLAN}
INDEX=${SLURM_ARRAY_TASK_ID:?this must run as a Slurm array task}
mapfile -t NODES < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
if (( ${#NODES[@]} < 2 )); then
  echo 'ERROR: dual-node E3 requires two allocated nodes' >&2
  exit 70
fi
SERVICE_NODE=${NODES[0]}
CLIENT_NODE=${NODES[1]}
CONTROL="$RUN_DIR/control/${SLURM_ARRAY_JOB_ID:-$SLURM_JOB_ID}_${INDEX}"
rm -rf "$CONTROL"
mkdir -p "$CONTROL" "$RUN_DIR/raw/services"
export PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done < <(jobs -pr)
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "START $(date -Is 2>/dev/null || date)"
echo "SERVICE_NODE=$SERVICE_NODE CLIENT_NODE=$CLIENT_NODE"
echo "TOPOLOGY dual-node-distinct-processes"
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" extract-block \
  --plan "$PLAN" --index "$INDEX" --output "$CONTROL/cases" > "$CONTROL/block.json"

for CASE_FILE in "$CONTROL"/cases/case-*.json; do
  eval "$("$ROOT/scripts/run_python.sh" "$ROOT/scripts/case_fields.py" "$CASE_FILE")"
  echo "CASE $CASE_ID kind=$KIND deployment=$DEPLOYMENT"
  if [[ "$KIND" == wire_audit ]]; then
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$CLIENT_NODE" --exclusive \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" client-case \
      --case-file "$CASE_FILE" --output "$RUN_DIR/raw"
    continue
  fi

  EXPECTED=$((MEASURED + WARMUP))
  SERVICE_PIDS=()
  if [[ "$DEPLOYMENT" == coalesced ]]; then
    GATEWAY_READY="$CONTROL/$CASE_ID.gateway.ready.json"
    GATEWAY_METRICS="$RUN_DIR/raw/services/$CASE_ID.gateway.server.json.gz"
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$SERVICE_NODE" --exclusive \
      --export=ALL,E3_ADVERTISE_HOST="$SERVICE_NODE" \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" serve \
      --case-file "$CASE_FILE" --role gateway --host 0.0.0.0 --port 0 \
      --ready-file "$GATEWAY_READY" --metrics-file "$GATEWAY_METRICS" \
      --expected-queries "$EXPECTED" &
    SERVICE_PIDS+=("$!")
    "$ROOT/scripts/run_python.sh" "$ROOT/scripts/wait_ready.py" "$GATEWAY_READY" --timeout 180
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$CLIENT_NODE" --exclusive \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" client-case \
      --case-file "$CASE_FILE" --output "$RUN_DIR/raw" \
      --gateway-ready "$GATEWAY_READY" --gateway-metrics "$GATEWAY_METRICS"
  else
    PAYLOAD_READY="$CONTROL/$CASE_ID.payload.ready.json"
    WITNESS_READY="$CONTROL/$CASE_ID.witness.ready.json"
    PAYLOAD_METRICS="$RUN_DIR/raw/services/$CASE_ID.payload.server.json.gz"
    WITNESS_METRICS="$RUN_DIR/raw/services/$CASE_ID.witness.server.json.gz"
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$SERVICE_NODE" --exclusive \
      --export=ALL,E3_ADVERTISE_HOST="$SERVICE_NODE" \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" serve \
      --case-file "$CASE_FILE" --role payload --host 0.0.0.0 --port 0 \
      --ready-file "$PAYLOAD_READY" --metrics-file "$PAYLOAD_METRICS" \
      --expected-queries "$EXPECTED" &
    SERVICE_PIDS+=("$!")
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$SERVICE_NODE" --exclusive \
      --export=ALL,E3_ADVERTISE_HOST="$SERVICE_NODE" \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" serve \
      --case-file "$CASE_FILE" --role witness --host 0.0.0.0 --port 0 \
      --ready-file "$WITNESS_READY" --metrics-file "$WITNESS_METRICS" \
      --expected-queries "$EXPECTED" &
    SERVICE_PIDS+=("$!")
    "$ROOT/scripts/run_python.sh" "$ROOT/scripts/wait_ready.py" \
      "$PAYLOAD_READY" "$WITNESS_READY" --timeout 180
    srun --nodes=1 --ntasks=1 --cpus-per-task=1 --nodelist="$CLIENT_NODE" --exclusive \
      "$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" client-case \
      --case-file "$CASE_FILE" --output "$RUN_DIR/raw" \
      --payload-ready "$PAYLOAD_READY" --payload-metrics "$PAYLOAD_METRICS" \
      --witness-ready "$WITNESS_READY" --witness-metrics "$WITNESS_METRICS"
  fi
  for pid in "${SERVICE_PIDS[@]}"; do wait "$pid"; done
done
trap - EXIT INT TERM
rm -rf "$CONTROL"
echo "END $(date -Is 2>/dev/null || date)"
