#!/usr/bin/env bash
set -euo pipefail
JOB_ID=${1:?usage: job_accounting.sh JOB_ID}
if command -v sacct >/dev/null 2>&1; then
  sacct -j "$JOB_ID" --units=M \
    --format=JobID,JobName%28,State,Elapsed,AllocNodes,AllocCPUS,MaxRSS,MaxVMSize,TotalCPU,ExitCode
else
  echo 'sacct is unavailable on this host.' >&2
  exit 69
fi
