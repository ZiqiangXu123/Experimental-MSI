#!/bin/bash -l
#SBATCH --job-name=msi-migration
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --partition=day
#SBATCH --time=01:00:00
#SBATCH --output=msi-migration-%j.out
set -eo pipefail
if [[ -z "${SLURM_JOB_ID:-}" ]]; then
    echo "Submit this file with sbatch." >&2
    exit 2
fi
module load Python/3.11.5-GCCcore-13.2.0
set -u
cd "${MSI_SUITE_DIR:-$SLURM_SUBMIT_DIR}"
if [[ ! -f run.sh ]]; then
    echo "Submit from the msi_pi_suite directory." >&2
    exit 2
fi
export MSI_KEEP_MIGRATION_WORKSPACES=1
MSI_CHECK_OUT="$PWD/outputs/hpc_migration_check_${SLURM_JOB_ID}"
if [[ -e "$MSI_CHECK_OUT" ]]; then
    echo "Refusing to reuse existing diagnostic output: $MSI_CHECK_OUT" >&2
    exit 2
fi
msi_check_finished() {
    MSI_CHECK_EXIT=$?
    echo "MSI_MIGRATION_EXIT_CODE=$MSI_CHECK_EXIT"
    echo "MSI_MIGRATION_RESULT_DIR=$MSI_CHECK_OUT"
}
trap msi_check_finished EXIT
python3 -c 'from msi_supplement.migration import ADAPTER_REVISION; print("Adapter:", ADAPTER_REVISION)'
echo "Workspace policy: retain experiment fixtures; original migration/recovery cleanup remains enabled."
echo "Same output filesystem; no redirection to node-local storage."
srun --ntasks=1 --cpus-per-task=1 --cpu-bind=cores python3 -m msi_supplement.migration --output "$MSI_CHECK_OUT" --profile paper --blocks 512 --block-bytes 2048 --repeats 5
python3 - "$MSI_CHECK_OUT" <<'PY'
import json, sys
from pathlib import Path
paths = list(Path(sys.argv[1]).glob('migration-*/records.jsonl'))
if len(paths) != 1:
    raise SystemExit('Expected exactly one migration result file.')
rows = [json.loads(s) for s in paths[0].read_text().splitlines() if s.strip()]
if len(rows) != 60 or any(r['status'] != 'ok' for r in rows):
    raise SystemExit('Migration results missing or failed; retain logs for diagnosis.')
if any(r['provenance'].get('workspace_cleanup_policy') != 'retain' for r in rows):
    raise SystemExit('Retention policy was not recorded for every case.')
print('MSI_MIGRATION_CASES=60')
print('MSI_MIGRATION_FAILED=0')
print('MSI_MIGRATION_CHECK=PASS')
PY
