#!/usr/bin/env bash
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
echo '=== timestamp ==='
date -Is 2>/dev/null || date
echo '=== host and kernel ==='
hostname -f 2>/dev/null || hostname
uname -a
echo '=== scheduler ==='
command -v sbatch || true
sbatch --version 2>/dev/null || true
sinfo -s 2>/dev/null || true
echo '=== Python candidates ==='
command -v python3 || true
python3 --version 2>&1 || true
echo '=== OpenSSL ==='
openssl version 2>&1 || true
echo '=== environment modules ==='
if type module >/dev/null 2>&1; then
  module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity' | head -100 || true
else
  for init in /etc/profile.d/modules.sh /usr/share/Modules/init/bash /etc/profile.d/lmod.sh; do
    if [[ -r "$init" ]]; then
      source "$init"
      break
    fi
  done
  type module >/dev/null 2>&1 && module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity' | head -100 || true
fi
echo '=== container engines ==='
command -v apptainer || true
command -v singularity || true
echo '=== CPU and memory ==='
command -v lscpu >/dev/null 2>&1 && lscpu || true
command -v free >/dev/null 2>&1 && free -h || true
echo '=== filesystems and quota ==='
df -hT "$ROOT" . 2>/dev/null || df -h "$ROOT" . 2>/dev/null || true
quota -s 2>/dev/null || true
echo '=== artifact runtime probe ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp04_scaling.pyz" info || true
