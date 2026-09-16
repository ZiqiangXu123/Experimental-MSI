#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo '=== E3 preflight: runtime and native Ed25519 ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" info

echo '=== E3 preflight: unit/integration tests ==='
bash "$ROOT/tests/run_tests.sh"

echo '=== E3 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e3-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" make-plan \
  --config "$ROOT/configs/publication.json" --output "$tmp/plan.jsonl" | tee "$tmp/summary.json"
blocks=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$tmp/plan.jsonl" --kind blocks)
cases=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$tmp/plan.jsonl" --kind cases)
network=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$tmp/plan.jsonl" --kind network)
wire=$("$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" count-plan --plan "$tmp/plan.jsonl" --kind wire)
[[ "$blocks" == 156 && "$cases" == 624 && "$network" == 560 && "$wire" == 64 ]] || {
  echo "ERROR: publication plan changed (blocks=$blocks cases=$cases network=$network wire=$wire)" >&2
  exit 65
}
"$ROOT/scripts/run_python.sh" - "$tmp/summary.json" <<'PY'
import json, pathlib, sys
obj=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert obj['minimum_network_trials_per_configuration'] == 5, obj
assert obj['maximum_network_trials_per_configuration'] == 5, obj
assert obj['total_measured_network_queries'] == 112000, obj
print('replication and query count: PASS')
PY

echo '=== E3 preflight: scheduler ==='
if ! command -v sbatch >/dev/null 2>&1; then
  echo 'ERROR: sbatch not found; run preflight on a Slurm login node.' >&2
  exit 69
fi
sbatch --version
sinfo -s || true

echo 'PREFLIGHT=PASS'
