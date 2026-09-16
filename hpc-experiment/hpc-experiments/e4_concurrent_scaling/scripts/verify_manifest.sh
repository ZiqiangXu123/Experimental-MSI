#!/usr/bin/env bash
set -euo pipefail
DIR=${1:?usage: verify_manifest.sh RESULTS_DIR}
[[ -f "$DIR/manifest.sha256" ]] || { echo "ERROR: missing $DIR/manifest.sha256" >&2; exit 66; }
(cd "$DIR" && sha256sum -c manifest.sha256)
