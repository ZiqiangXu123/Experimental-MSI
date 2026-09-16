#!/usr/bin/env bash
set -euo pipefail
RESULT_DIR=${1:?usage: verify_manifest.sh RESULT_DIR}
cd "$RESULT_DIR"
sha256sum -c manifest.sha256
