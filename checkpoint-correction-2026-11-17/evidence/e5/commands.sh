#!/usr/bin/env bash
set -euo pipefail
# Run from the manuscript workspace root. On Linux, omit DYLD_LIBRARY_PATH
# if the normal libcrypto supplies Ed25519.
E5_PYTHON=<PYTHON_EXECUTABLE>
export DYLD_LIBRARY_PATH=<OPENSSL_LIBRARY_DIRECTORY>
E5_ROOT=Technical_Consistency_2026-11-17
PYTHONPATH="$E5_ROOT/code_revised/experiment/experiments/e5_anchor_pruning/src" "$E5_PYTHON" -m unittest discover -s "$E5_ROOT/code_revised/experiment/experiments/e5_anchor_pruning/tests" -p 'test_*.py' -v > "$E5_ROOT/working/e5/tests.log" 2>&1
PYTHONPATH="$E5_ROOT/code_original/experiment/experiments/e5_anchor_pruning/src" "$E5_PYTHON" "$E5_ROOT/working/e5/collect_regression.py" > "$E5_ROOT/working/e5/regression_before.json"
PYTHONPATH="$E5_ROOT/code_revised/experiment/experiments/e5_anchor_pruning/src" "$E5_PYTHON" "$E5_ROOT/working/e5/collect_regression.py" > "$E5_ROOT/working/e5/regression_after.json"
