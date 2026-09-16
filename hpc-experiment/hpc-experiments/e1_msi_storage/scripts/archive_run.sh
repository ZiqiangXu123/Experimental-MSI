#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: archive_run.sh RUN_DIR [OUTPUT.tar.gz]}
OUTPUT=${2:-$(basename "$RUN_DIR").tar.gz}
if [[ ! -d "$RUN_DIR/results" ]]; then
  echo "ERROR: result directory not found under $RUN_DIR" >&2
  exit 66
fi
tar -C "$(dirname "$RUN_DIR")" -czf "$OUTPUT" "$(basename "$RUN_DIR")"
sha256sum "$OUTPUT" > "$OUTPUT.sha256"
echo "ARCHIVE=$OUTPUT"
echo "CHECKSUM=$OUTPUT.sha256"
