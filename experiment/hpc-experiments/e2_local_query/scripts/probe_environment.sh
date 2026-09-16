#!/usr/bin/env bash
set -u

echo '=== timestamp and identity ==='
date -Is 2>/dev/null || date
id
hostname -f 2>/dev/null || hostname
pwd

echo '=== Slurm ==='
command -v sbatch || true
sbatch --version 2>/dev/null || true
sinfo -s 2>/dev/null || true

echo '=== Python ==='
command -v python3 || true
python3 --version 2>&1 || true

echo '=== environment modules ==='
if type module >/dev/null 2>&1; then
  module -t avail 2>&1 | grep -Ei 'python|apptainer|singularity|openssl' || true
else
  echo 'module command is not initialised in this shell'
fi

echo '=== rootless container runtimes ==='
command -v apptainer || true
apptainer --version 2>/dev/null || true
command -v singularity || true
singularity --version 2>/dev/null || true

echo '=== OpenSSL/libcrypto ==='
command -v openssl || true
openssl version -a 2>/dev/null || true
ldconfig -p 2>/dev/null | grep -E 'libcrypto\.so' || true

echo '=== CPU and affinity ==='
command -v lscpu >/dev/null 2>&1 && lscpu || true
grep -E 'Cpus_allowed_list|Mems_allowed_list' /proc/self/status 2>/dev/null || true
for f in /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor \
         /proc/sys/kernel/perf_event_paranoid; do
  [[ -r "$f" ]] && echo "$f=$(cat "$f")"
done

echo '=== Linux package energy interfaces ==='
find /sys/class/powercap -maxdepth 3 -type f \
  \( -name energy_uj -o -name max_energy_range_uj -o -name name \) \
  -print 2>/dev/null | sort || true

echo '=== storage and quota ==='
df -hT . 2>/dev/null || df -h .
quota -s 2>/dev/null || true

echo '=== process limits ==='
ulimit -a
