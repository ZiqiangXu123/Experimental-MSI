#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

echo '=== E1 preflight: implementation ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" info

echo '=== E1 preflight: unit tests ==='
bash "$ROOT/tests/run_tests.sh"

echo '=== E1 preflight: publication plan ==='
tmp=$(mktemp -d "${TMPDIR:-/tmp}/msi-e1-preflight.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
"$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" make-plan \
  --config "$ROOT/configs/publication.json" \
  --output "$tmp/plan.jsonl"
points=$("$ROOT/scripts/run_python.sh" "$ROOT/exp01_storage.pyz" count-plan --plan "$tmp/plan.jsonl")
if [[ "$points" != 80 ]]; then
  echo "ERROR: expected 80 publication points, found $points" >&2
  exit 65
fi

echo '=== E1 preflight: scheduler ==='
if ! command -v sbatch >/dev/null 2>&1; then
  echo 'ERROR: sbatch not found; run this on a Slurm login node.' >&2
  exit 69
fi
sbatch --version
sinfo -s || true

echo 'PREFLIGHT=PASS'
