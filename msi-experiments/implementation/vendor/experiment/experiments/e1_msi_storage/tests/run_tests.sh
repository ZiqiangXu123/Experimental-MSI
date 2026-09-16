#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHONPATH="$ROOT/src" "$ROOT/scripts/run_python.sh" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
