#!/usr/bin/env bash
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
echo '=== host and time ==='
date -Is 2>/dev/null || date
hostname -f 2>/dev/null || hostname
uname -a
echo '=== scheduler ==='
command -v sbatch || true
sbatch --version 2>/dev/null || true
sinfo -s 2>/dev/null || true
echo '=== Python/module/container candidates ==='
command -v python3 || true
python3 --version 2>&1 || true
if type module >/dev/null 2>&1; then module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity' | head -80 || true; fi
command -v apptainer || true
command -v singularity || true
echo '=== crypto ==='
openssl version 2>/dev/null || true
"$ROOT/scripts/run_python.sh" "$ROOT/exp03_network.pyz" info 2>&1 || true
echo '=== user namespaces and network utilities (optional only) ==='
command -v unshare || true
command -v tc || true
sysctl kernel.unprivileged_userns_clone 2>/dev/null || true
echo '=== storage/quota ==='
df -hT . 2>/dev/null || df -h .
quota -s 2>/dev/null || true
echo '=== limits ==='
ulimit -a
