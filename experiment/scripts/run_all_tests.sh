#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for dir in "$ROOT"/experiments/*; do
  echo "TEST_COMPONENT=$(basename "$dir")"
  bash "$dir/tests/run_tests.sh"
done
