#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: archive_run.sh RUN_DIR [OUTPUT.tar.gz]}
OUTPUT=${2:-$(basename "$RUN_DIR").tar.gz}
[[ -d "$RUN_DIR/results" ]] || { echo "ERROR: $RUN_DIR/results does not exist" >&2; exit 66; }
tar -C "$(dirname "$RUN_DIR")" -czf "$OUTPUT" "$(basename "$RUN_DIR")"
sha256sum "$OUTPUT" > "$OUTPUT.sha256"
echo "ARCHIVE=$OUTPUT"
echo "CHECKSUM=$OUTPUT.sha256"
