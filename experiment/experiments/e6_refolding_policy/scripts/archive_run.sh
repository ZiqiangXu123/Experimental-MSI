#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: archive_run.sh RUN_DIR}
RUN_DIR=$(cd "$RUN_DIR" && pwd)
PARENT=$(dirname "$RUN_DIR")
NAME=$(basename "$RUN_DIR")
ARCHIVE=${2:-$PARENT/$NAME.tar.gz}
tar --exclude="$NAME/datasets" --exclude="$NAME/shared-work" -C "$PARENT" -czf "$ARCHIVE" "$NAME"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
printf 'ARCHIVE=%s\nSHA256=%s\n' "$ARCHIVE" "$ARCHIVE.sha256"
