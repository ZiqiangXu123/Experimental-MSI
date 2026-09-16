#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo '=== E5 preflight: artifact hashes ==='
"$ROOT/scripts/verify_artifact.sh"

echo '=== E5 preflight: Python and OpenSSL Ed25519 ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" info

echo '=== E5 preflight: regression and integration tests ==='
cd "$ROOT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT/scripts/run_python.sh" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

echo '=== E5 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e5-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" make-plan \
  --config "$ROOT/configs/publication.json" --output "$tmp/plan.jsonl" | tee "$tmp/plan_summary.json"
cases=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" count-plan --plan "$tmp/plan.jsonl" --kind cases)
configs=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" count-plan --plan "$tmp/plan.jsonl" --kind configurations)
queries=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" count-plan --plan "$tmp/plan.jsonl" --kind query_measurements)
epochs=$("$ROOT/scripts/run_python.sh" "$ROOT/exp05_anchor.pyz" count-plan --plan "$tmp/plan.jsonl" --kind pruned_epoch_inputs)
[[ "$cases" == 552 && "$configs" == 80 && "$queries" == 1200000 && "$epochs" == 460400 ]] || {
  echo "ERROR: publication plan changed (cases=$cases configs=$configs queries=$queries epoch_inputs=$epochs)." >&2
  exit 65
}
"$ROOT/scripts/run_python.sh" - "$tmp/plan.jsonl" <<'PY'
import collections, json, sys
rows=[json.loads(line) for line in open(sys.argv[1],encoding='utf-8') if line.strip()]
by_kind=collections.Counter(row['trial_kind'] for row in rows)
assert by_kind == {'anchor_query':120,'checkpoint_update':200,'pruning':230,'unsafe_counterexample':2}, by_kind
replicas=collections.Counter(row['config_id'] for row in rows)
assert len(replicas)==80
assert set(replicas.values()) <= {1,5,10}
print('publication plan invariants: PASS')
PY

echo '=== E5 preflight: scheduler ==='
command -v sbatch >/dev/null 2>&1 || {
  echo 'ERROR: sbatch not found; run preflight on a Slurm login node.' >&2
  exit 69
}
sbatch --version
sinfo -s || true
echo 'PREFLIGHT=PASS'
