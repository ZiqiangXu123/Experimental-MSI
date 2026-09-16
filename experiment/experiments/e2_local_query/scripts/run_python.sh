#!/usr/bin/env bash
set -euo pipefail

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

initialise_modules() {
  if type module >/dev/null 2>&1; then
    return 0
  fi
  for init in /etc/profile.d/modules.sh /usr/share/Modules/init/bash /etc/profile.d/lmod.sh; do
    if [[ -r "$init" ]]; then
      source "$init"
      break
    fi
  done
}

run_container() {
  local engine=""
  if command -v apptainer >/dev/null 2>&1; then
    engine=$(command -v apptainer)
  elif command -v singularity >/dev/null 2>&1; then
    engine=$(command -v singularity)
  fi
  if [[ -z "$engine" ]]; then
    echo "ERROR: EXP02_CONTAINER is set, but apptainer/singularity is unavailable" >&2
    exit 72
  fi
  if [[ ! -r "$EXP02_CONTAINER" ]]; then
    echo "ERROR: EXP02_CONTAINER is not readable: $EXP02_CONTAINER" >&2
    exit 72
  fi
  local script_root
  script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  local -a bind_args=(--bind "$PWD:$PWD")
  [[ "$script_root" != "$PWD" ]] && bind_args+=(--bind "$script_root:$script_root")
  for candidate in "${SLURM_TMPDIR:-}" "${RUN_BASE:-}"; do
    [[ -n "$candidate" && -d "$candidate" ]] && bind_args+=(--bind "$candidate:$candidate")
  done
  exec "$engine" exec "${bind_args[@]}" --pwd "$PWD" \
    "$EXP02_CONTAINER" python3 "$@"
}

if [[ -n "${PYTHON_BIN:-}" ]]; then
  if python_ok "$PYTHON_BIN"; then
    exec "$PYTHON_BIN" "$@"
  fi
  echo "ERROR: PYTHON_BIN=$PYTHON_BIN is not Python >= 3.10" >&2
  exit 70
fi

if [[ -n "${PYTHON_MODULE:-}" ]]; then
  initialise_modules
  if ! type module >/dev/null 2>&1; then
    echo "ERROR: PYTHON_MODULE is set, but Environment Modules/Lmod is unavailable" >&2
    exit 71
  fi
  module load "$PYTHON_MODULE"
  if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
    exec "$(command -v python3)" "$@"
  fi
  echo "ERROR: module '$PYTHON_MODULE' did not provide Python >= 3.10" >&2
  exit 71
fi

if [[ -n "${EXP02_CONTAINER:-}" ]]; then
  run_container "$@"
fi

if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
  exec "$(command -v python3)" "$@"
fi

cat >&2 <<'MSG'
ERROR: Python >= 3.10 was not found.

No administrator installation is required. Use one user-level path:
  1. Load a site module:
       module -t avail 2>&1 | grep -i python
       export PYTHON_MODULE='EXACT/MODULE/NAME'
  2. Select an existing interpreter:
       export PYTHON_BIN=/path/to/python3
  3. Use an uploaded rootless Apptainer/Singularity image:
       export EXP02_CONTAINER=$HOME/containers/python-3.12-bookworm.sif
MSG
exit 69
