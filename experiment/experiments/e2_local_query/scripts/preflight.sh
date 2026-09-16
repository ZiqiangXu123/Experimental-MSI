#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo '=== E2 preflight: native crypto and counters ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" info

echo '=== E2 preflight: unit tests ==='
bash "$ROOT/tests/run_tests.sh"

echo '=== E2 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e2-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" make-plan \
  --config "$ROOT/configs/publication.json" \
  --output "$tmp/plan.jsonl" | tee "$tmp/plan_summary.json"
blocks=$("$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" count-plan --plan "$tmp/plan.jsonl" --kind blocks)
cases=$("$ROOT/scripts/run_python.sh" "$ROOT/exp02_query.pyz" count-plan --plan "$tmp/plan.jsonl" --kind cases)
[[ "$blocks" == 290 ]] || { echo "ERROR: expected 290 blocks, found $blocks" >&2; exit 65; }
[[ "$cases" == 1330 ]] || { echo "ERROR: expected 1330 cases, found $cases" >&2; exit 65; }
"$ROOT/scripts/run_python.sh" - "$tmp/plan_summary.json" <<'PY'
import json, pathlib, sys
obj=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert obj['minimum_launches_per_configuration'] == 10, obj
assert obj['maximum_launches_per_configuration'] == 10, obj
print('launch replication: PASS (exactly 10 per configuration)')
PY

echo '=== E2 preflight: scheduler ==='
if ! command -v sbatch >/dev/null 2>&1; then
  echo 'ERROR: sbatch not found; run this on a Slurm login node.' >&2
  exit 69
fi
sbatch --version
sinfo -s || true

echo 'PREFLIGHT=PASS'
