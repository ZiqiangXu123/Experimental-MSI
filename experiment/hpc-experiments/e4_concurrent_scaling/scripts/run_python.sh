#!/usr/bin/env bash
set -euo pipefail

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

initialise_modules() {
  type module >/dev/null 2>&1 && return 0
  for init in /etc/profile.d/modules.sh /usr/share/Modules/init/bash /etc/profile.d/lmod.sh; do
    if [[ -r "$init" ]]; then
      source "$init"
      break
    fi
  done
}

run_container() {
  local engine=''
  if command -v apptainer >/dev/null 2>&1; then
    engine=$(command -v apptainer)
  elif command -v singularity >/dev/null 2>&1; then
    engine=$(command -v singularity)
  fi
  [[ -n "$engine" ]] || {
    echo 'ERROR: EXP04_CONTAINER is set but Apptainer/Singularity is unavailable.' >&2
    exit 72
  }
  [[ -r "$EXP04_CONTAINER" ]] || {
    echo "ERROR: unreadable container: $EXP04_CONTAINER" >&2
    exit 72
  }
  local script_root
  script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  local -a binds=(--bind "$PWD:$PWD")
  [[ "$script_root" != "$PWD" ]] && binds+=(--bind "$script_root:$script_root")
  for candidate in "${SLURM_TMPDIR:-}" "${RUN_BASE:-}" "${RUN_DIR:-}"; do
    [[ -n "$candidate" && -d "$candidate" ]] && binds+=(--bind "$candidate:$candidate")
  done
  exec "$engine" exec "${binds[@]}" --pwd "$PWD" "$EXP04_CONTAINER" python3 "$@"
}

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_ok "$PYTHON_BIN" && exec "$PYTHON_BIN" "$@"
  echo "ERROR: PYTHON_BIN=$PYTHON_BIN is not Python >=3.10." >&2
  exit 70
fi

if [[ -n "${PYTHON_MODULE:-}" ]]; then
  initialise_modules
  type module >/dev/null 2>&1 || {
    echo 'ERROR: module command unavailable.' >&2
    exit 71
  }
  module load "$PYTHON_MODULE"
  if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
    exec "$(command -v python3)" "$@"
  fi
  echo "ERROR: module '$PYTHON_MODULE' did not provide Python >=3.10." >&2
  exit 71
fi

[[ -n "${EXP04_CONTAINER:-}" ]] && run_container "$@"

if command -v python3 >/dev/null 2>&1 && python_ok "$(command -v python3)"; then
  exec "$(command -v python3)" "$@"
fi

cat >&2 <<'MSG'
ERROR: Python >=3.10 was not found. Administrator installation is not required.
Choose one option:
  export PYTHON_MODULE='EXACT/MODULE/NAME'
  export PYTHON_BIN=/path/to/python3
  export EXP04_CONTAINER=$HOME/containers/python-3.12-bookworm.sif
MSG
exit 69
