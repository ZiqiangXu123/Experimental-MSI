#!/usr/bin/env bash
set -euo pipefail
TEST_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHONPATH="$TEST_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1 "${CHECKPOINT_PYTHON:-python3}" -m unittest discover -s "$TEST_ROOT/tests" -p 'test_*.py' -v
