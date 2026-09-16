#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"

echo '=== E7 preflight: artifact hashes ==='
"$ROOT/scripts/verify_artifact.sh"

echo '=== E7 preflight: Python and OpenSSL Ed25519 ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" info

echo '=== E7 preflight: regression and integration tests ==='
cd "$ROOT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT/scripts/run_python.sh" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

echo '=== E7 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e7-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp07_security_availability.pyz" plan \
  --config "$ROOT/configs/publication.json" --output "$tmp" > "$tmp/plan_output.json"
"$ROOT/scripts/run_python.sh" - "$tmp/plan_summary.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1],encoding='utf-8'))
expected={
 'e7_cases':247,
 'e7_blocks':247,
 'e7_deterministic_blocks':200,
 'e7_fuzz_blocks':32,
 'e7_ablation_blocks':15,
 'e7_attack_scenarios':1_000_000,
 'e7_malicious_responses':1_164_000,
 'e7_honest_controls':1_179_000,
 'e7_fuzz_trials':64_000,
 'e7_ablation_trials':15_000,
 'e8_cases':7200,
 'e8_blocks':360,
 'e8_simulated_queries':72_000_000,
}
for key,value in expected.items():
    assert s[key]==value,(key,s[key],value)
assert all(v==100_000 for v in s['e7_scenarios_by_family'].values())
print('publication plan invariants: PASS')
PY

echo '=== E7 preflight: scheduler ==='
command -v sbatch >/dev/null 2>&1 || { echo 'ERROR: sbatch not found; run preflight on a Slurm login node.' >&2; exit 69; }
sbatch --version
sinfo -s || true

echo 'PREFLIGHT=PASS'
