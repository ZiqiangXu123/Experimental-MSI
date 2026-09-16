#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/scripts/site_env.sh" ]] && source "$ROOT/scripts/site_env.sh"

echo '=== E6 preflight: artifact hashes ==='
"$ROOT/scripts/verify_artifact.sh"

echo '=== E6 preflight: Python and OpenSSL Ed25519 ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" info

echo '=== E6 preflight: regression and integration tests ==='
cd "$ROOT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT/scripts/run_python.sh" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v

echo '=== E6 preflight: publication-plan audit ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e6-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" plan \
  --config "$ROOT/configs/publication.json" --output "$tmp" | tee "$tmp/plan_output.json"
"$ROOT/scripts/run_python.sh" - "$tmp/plan_summary.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1],encoding='utf-8'))
expected={
 'datasets':22,
 'migration_cases':1410,
 'migration_blocks':353,
 'policy_cases':590,
 'policy_blocks':118,
 'policy_replicates':2770,
 'policy_algorithm_rows':19120,
}
for key,value in expected.items():
    assert s[key]==value,(key,s[key],value)
assert s['migration_case_kinds']=={'migration_perf':1176,'equivalence_audit':96,'fault_injection':138}
print('publication plan invariants: PASS')
PY

echo '=== E6 preflight: component catalog ==='
if [[ -n "${E6_COMPONENT_COST_CATALOG:-}" ]]; then
  "$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" validate-component-catalog \
    --catalog "$E6_COMPONENT_COST_CATALOG" --n-values 64,512,4096,16384 --require-publication
  echo 'PUBLICATION_CATALOG=PASS'
else
  echo 'PUBLICATION_CATALOG=NOT_CONFIGURED'
  echo 'NOTE: smoke/reference runs are available, but submit_publication.sh will stop until E6_COMPONENT_COST_CATALOG is set.'
fi

echo '=== E6 preflight: scheduler ==='
command -v sbatch >/dev/null 2>&1 || {
  echo 'ERROR: sbatch not found; run preflight on a Slurm login node.' >&2
  exit 69
}
sbatch --version
sinfo -s || true

echo 'PREFLIGHT=PASS'
