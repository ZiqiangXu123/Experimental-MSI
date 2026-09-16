#!/usr/bin/env bash
set -u

echo "=== timestamp ==="
date -Is 2>/dev/null || date

echo "=== identity ==="
id
hostname -f 2>/dev/null || hostname
pwd

echo "=== scheduler ==="
command -v sbatch || true
sbatch --version 2>/dev/null || true
sinfo -s 2>/dev/null || true

echo "=== Python ==="
command -v python3 || true
python3 --version 2>&1 || true

echo "=== modules matching Python/container tools ==="
if type module >/dev/null 2>&1; then
  module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity' || true
else
  echo "module command is not initialized in this shell"
fi

echo "=== container runtimes ==="
command -v apptainer || true
command -v singularity || true

echo "=== storage/quota ==="
df -hT . 2>/dev/null || df -h .
quota -s 2>/dev/null || true

echo "=== limits ==="
ulimit -a
