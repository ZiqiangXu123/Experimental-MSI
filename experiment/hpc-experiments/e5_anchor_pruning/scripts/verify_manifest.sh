#!/usr/bin/env bash
set -euo pipefail
RESULTS_DIR=${1:?usage: verify_manifest.sh RESULTS_DIR}
RESULTS_DIR=$(cd "$RESULTS_DIR" && pwd)
[[ -f "$RESULTS_DIR/manifest.sha256" ]] || { echo 'ERROR: manifest.sha256 is missing.' >&2; exit 66; }
(cd "$RESULTS_DIR" && sha256sum -c manifest.sha256)
