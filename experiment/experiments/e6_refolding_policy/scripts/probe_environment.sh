#!/usr/bin/env bash
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
echo '=== timestamp ==='; date -Ins 2>/dev/null || date
echo '=== host and kernel ==='; hostname; uname -a
echo '=== current directory and filesystem ==='; pwd; df -hT . 2>/dev/null || df -h .
echo '=== quota (site-dependent) ==='; quota -s 2>&1 || true
echo '=== Python candidates ===';
for exe in python3 python; do command -v "$exe" 2>/dev/null && "$exe" --version 2>&1; done
echo '=== module command and available runtimes ===';
type module 2>&1 || true
(module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity' | head -80) || true
echo '=== OpenSSL ==='; openssl version -a 2>&1 || true
echo '=== rootless container engines ===';
command -v apptainer 2>/dev/null || true; command -v singularity 2>/dev/null || true
echo '=== Slurm ===';
command -v sbatch 2>/dev/null || true; sbatch --version 2>&1 || true; sinfo -s 2>&1 || true
echo '=== candidate node-local scratch ===';
printf 'SLURM_TMPDIR=%s\nTMPDIR=%s\n' "${SLURM_TMPDIR:-}" "${TMPDIR:-}"
echo '=== resource limits ==='; ulimit -a 2>&1 || true
echo '=== artifact runtime self-test ==='
"$ROOT/scripts/run_python.sh" "$ROOT/exp06_refolding_policy.pyz" info 2>&1 || true
