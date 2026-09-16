#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo '=== E4 preflight: Python and OpenSSL Ed25519 ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" info

echo '=== E4 preflight: 10 regression and integration tests ==='
cd "$ROOT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT/scripts/run_python.sh" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

echo '=== E4 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e4-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" make-plan \
  --config "$ROOT/configs/publication.json" --output "$tmp/plan.jsonl" | tee "$tmp/plan_summary.json"
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" make-dataset-plan \
  --plan "$tmp/plan.jsonl" --output "$tmp/datasets.jsonl" | tee "$tmp/dataset_summary.json"
cases=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" count-plan --plan "$tmp/plan.jsonl" --kind cases)
datasets=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" count-plan --plan "$tmp/plan.jsonl" --kind datasets)
steps=$("$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" count-plan --plan "$tmp/plan.jsonl" --kind rate_steps)
[[ "$cases" == 400 && "$datasets" == 10 && "$steps" == 975 ]] || {
  echo "ERROR: publication plan changed (cases=$cases datasets=$datasets rate_steps=$steps)." >&2
  exit 65
}
"$ROOT/scripts/run_python.sh" - "$tmp/plan.jsonl" <<'PY'
import json, sys
counts = {}
with open(sys.argv[1], encoding='utf-8') as handle:
    for line in handle:
        row = json.loads(line)
        counts[row['config_id']] = counts.get(row['config_id'], 0) + 1
assert len(counts) == 80, len(counts)
assert set(counts.values()) == {5}, set(counts.values())
print('independent trial replication: PASS (exactly 5 per configuration)')
PY

echo '=== E4 preflight: scheduler ==='
command -v sbatch >/dev/null 2>&1 || {
  echo 'ERROR: sbatch not found; run preflight on a Slurm login node.' >&2
  exit 69
}
sbatch --version
sinfo -s || true
echo 'PREFLIGHT=PASS'
