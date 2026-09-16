#!/usr/bin/env bash
set -euo pipefail

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
}

if [[ -n "${PYTHON_BIN:-}" ]]; then
  if python_ok "$PYTHON_BIN"; then
    exec "$PYTHON_BIN" "$@"
  fi
  echo "ERROR: PYTHON_BIN=$PYTHON_BIN is not Python >= 3.9" >&2
  exit 70
fi

if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
  exec "$(command -v python3)" "$@"
fi

if ! type module >/dev/null 2>&1; then
  for init in /etc/profile.d/modules.sh /usr/share/Modules/init/bash /etc/profile.d/lmod.sh; do
    if [[ -r "$init" ]]; then
      source "$init"
      break
    fi
  done
fi

if [[ -n "${PYTHON_MODULE:-}" ]] && type module >/dev/null 2>&1; then
  module load "$PYTHON_MODULE"
  if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
    exec "$(command -v python3)" "$@"
  fi
  echo "ERROR: module '$PYTHON_MODULE' did not provide Python >= 3.9" >&2
  exit 71
fi

if [[ -n "${EXP01_CONTAINER:-}" ]]; then
  engine=""
  if command -v apptainer >/dev/null 2>&1; then
    engine="$(command -v apptainer)"
  elif command -v singularity >/dev/null 2>&1; then
    engine="$(command -v singularity)"
  fi
  if [[ -n "$engine" ]]; then
    bind_args=(--bind "$PWD:$PWD")
    script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    [[ "$script_root" != "$PWD" ]] && bind_args+=(--bind "$script_root:$script_root")
    for candidate in "${SLURM_TMPDIR:-}" "${RUN_BASE:-}" "${EXP01_SCRATCH:-}"; do
      [[ -n "$candidate" && -d "$candidate" ]] && bind_args+=(--bind "$candidate:$candidate")
    done
    exec "$engine" exec "${bind_args[@]}" --pwd "$PWD" "$EXP01_CONTAINER" python3 "$@"
  fi
  echo "ERROR: EXP01_CONTAINER is set, but neither apptainer nor singularity is available" >&2
  exit 72
fi

cat >&2 <<'EOF'
ERROR: Python >= 3.9 was not found.

No administrator installation is required. Use one of these user-level paths:
  1. Load an available module and export its exact name:
       module avail 2>&1 | grep -i python
       export PYTHON_MODULE='THE/EXACT/MODULE'
  2. Set an existing interpreter explicitly:
       export PYTHON_BIN=/path/to/python3
  3. Use an uploaded Apptainer/Singularity image:
       export EXP01_CONTAINER=$HOME/containers/python-3.12.sif
EOF
exit 69
