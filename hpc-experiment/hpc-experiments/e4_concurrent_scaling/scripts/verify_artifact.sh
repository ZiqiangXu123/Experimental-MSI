#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$ROOT/ARTIFACT_MANIFEST.sha256" ]] || { echo 'ERROR: ARTIFACT_MANIFEST.sha256 is missing.' >&2; exit 66; }
(cd "$ROOT" && sha256sum -c ARTIFACT_MANIFEST.sha256)
