#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: archive_run.sh RUN_DIR [OUTPUT_PREFIX]}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
PREFIX=${2:-$(dirname "$RUN_DIR")/$(basename "$RUN_DIR")}
ARCHIVE="${PREFIX}.tar.gz"
tar -C "$(dirname "$RUN_DIR")" -czf "$ARCHIVE" "$(basename "$RUN_DIR")"
sha256sum "$ARCHIVE" > "${ARCHIVE}.sha256"
echo "$ARCHIVE"
echo "${ARCHIVE}.sha256"
