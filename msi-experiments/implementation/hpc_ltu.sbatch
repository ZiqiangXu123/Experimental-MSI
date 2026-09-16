#!/bin/bash -l
#SBATCH --job-name=msi-hpc
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=04:00:00
#SBATCH --output=msi-hpc-%j.out

set -eo pipefail
if [[ -z "${SLURM_JOB_ID:-}" ]]; then
    echo "Submit with sbatch; this wrapper runs experiments on an allocated compute node." >&2
    exit 2
fi
if ! type module >/dev/null 2>&1; then
    echo "The cluster module command is unavailable in this batch shell." >&2
    exit 2
fi
module load Python/3.11.5-GCCcore-13.2.0
set -u

MSI_ROOT="${MSI_SUITE_DIR:-${SLURM_SUBMIT_DIR:-$PWD}}"
cd "$MSI_ROOT"
MSI_ROOT="$PWD"
if [[ ! -f run.sh ]]; then
    echo "Submit from the msi_pi_suite directory or set MSI_SUITE_DIR." >&2
    exit 2
fi
MSI_MODE="${1:-smoke}"
if (( $# > 0 )); then shift; fi
MSI_RUN_DIR="$MSI_ROOT/outputs/hpc_${MSI_MODE}_${SLURM_JOB_ID}"
MSI_LOG_DIR="$MSI_ROOT/hpc_job_logs/${SLURM_JOB_ID}"
mkdir -p "$MSI_LOG_DIR"

msi_finished() {
    MSI_EXIT_STATUS=$?
    echo "MSI_JOB_EXIT_CODE=$MSI_EXIT_STATUS"
    echo "MSI_RESULT_DIR=$MSI_RUN_DIR"
    echo "MSI_JOB_LOG_DIR=$MSI_LOG_DIR"
}
trap msi_finished EXIT

msi_step() {
    srun --ntasks=1 --cpus-per-task=1 --cpu-bind=cores "$@"
}

echo "MSI_MODE=$MSI_MODE"
echo "MSI_RESULT_DIR=$MSI_RUN_DIR"
python3 --version
bash run.sh --version
module list > "$MSI_LOG_DIR/modules.txt" 2>&1
msi_step bash run.sh doctor > "$MSI_LOG_DIR/doctor.json"
export MSI_LTU_MODE="$MSI_MODE"
msi_step python3 - "$MSI_LOG_DIR/job_info.json" <<'PY'
import json, os, platform, sys, time
from pathlib import Path
keys = ('SLURM_JOB_ID', 'SLURM_JOB_NODELIST', 'SLURM_CPUS_PER_TASK',
        'SLURM_MEM_PER_NODE', 'SLURM_JOB_PARTITION', 'SLURM_JOB_ACCOUNT', 'MSI_LTU_MODE')
info = {'recorded_unix_ns': time.time_ns(), 'python': sys.version,
        'architecture': platform.machine(), 'cpu_affinity': sorted(os.sched_getaffinity(0)),
        'slurm': {k: os.environ.get(k) for k in keys},
        'scope': 'Actual job environment; requested RAM is not measured process RSS.'}
Path(sys.argv[1]).write_text(json.dumps(info, indent=2) + '\n')
PY

case "$MSI_MODE" in
    smoke)
        if (( $# != 0 )); then echo "smoke takes no extra arguments" >&2; exit 2; fi
        msi_step bash run.sh test
        msi_step bash run.sh run --role hpc --profile smoke --out "$MSI_RUN_DIR"
        ;;
    synthetic)
        if (( $# != 0 )); then echo "synthetic takes no extra arguments" >&2; exit 2; fi
        msi_step bash run.sh run --role hpc --profile paper --out "$MSI_RUN_DIR"
        ;;
    real)
        if (( $# > 1 )); then echo "Usage: sbatch hpc_ltu.sbatch real [DATASET_DIR]" >&2; exit 2; fi
        MSI_DATASET="${1:-datasets/ethereum}"
        msi_step python3 - "$MSI_DATASET" <<'PY'
import sys
from pathlib import Path
from msi_supplement.data import load_dataset
manifest, payloads = load_dataset(Path(sys.argv[1]))
if manifest.get('data_kind') != 'ethereum_rpc_finalized_json':
    raise SystemExit('real mode requires the saved Ethereum RPC dataset, not synthetic input.')
print('Dataset validated:', manifest['data_kind'], 'downloaded blocks:', len(payloads),
      'blocks_sha256:', manifest['blocks_sha256'])
print('The suite records the actual bounded prefix selected for measurement.')
PY
        msi_step bash run.sh run --role hpc --profile paper --out "$MSI_RUN_DIR" --dataset "$MSI_DATASET"
        ;;
    scale)
        if (( $# != 0 )); then echo "scale takes no extra arguments" >&2; exit 2; fi
        msi_step bash run.sh run --role hpc --profile paper --out "$MSI_RUN_DIR" --sizes 16384 --skip-migration
        ;;
    compare)
        if (( $# < 2 )); then echo "Usage: sbatch hpc_ltu.sbatch compare PI_RUN HPC_RUN [MORE_RUNS]" >&2; exit 2; fi
        msi_step python3 - "$@" <<'PY'
import json, sys
from pathlib import Path
for name in sys.argv[1:]:
    manifest = json.loads((Path(name) / 'run_manifest.json').read_text())
    if manifest.get('profile') != 'paper':
        raise SystemExit('Only paper runs belong in this comparison: ' + name)
    if not manifest.get('completed_unix_ns') or manifest.get('fatal_error') or manifest.get('failed_records', 0):
        raise SystemExit('Run is incomplete or contains failures; review it before comparison: ' + name)
print('Comparison inputs are completed paper runs without recorded fatal/measurement failures.')
PY
        msi_step bash run.sh compare --out "$MSI_RUN_DIR" "$@"
        exit 0
        ;;
    *)
        echo "Mode must be smoke, synthetic, real, scale, or compare." >&2
        exit 2
        ;;
esac

msi_step python3 - "$MSI_RUN_DIR" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / 'run_manifest.json').read_text())
rows = [json.loads(line) for line in (root / 'results.jsonl').read_text().splitlines() if line.strip()]
counts = dict(Counter(row['status'] for row in rows))
print('MSI_RECORD_COUNTS=' + json.dumps(counts, sort_keys=True))
print('MSI_FAILED_RECORDS=' + str(manifest.get('failed_records', 'missing')))
if not manifest.get('completed_unix_ns') or manifest.get('fatal_error') or manifest.get('failed_records', 0):
    raise SystemExit('Run is incomplete or contains failures; preserve the output for diagnosis.')
print('MSI_CHECK=PASS')
PY
