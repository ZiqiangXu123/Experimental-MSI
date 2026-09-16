#!/usr/bin/env bash
set -euo pipefail
JOB_ID=${1:?usage: job_accounting.sh JOB_ID}
command -v sacct >/dev/null 2>&1 || { echo 'ERROR: sacct is unavailable on this system.' >&2; exit 69; }
sacct -j "$JOB_ID" --units=K --format=JobID,JobName%24,State,Elapsed,AllocCPUS,MaxRSS,MaxVMSize,CPUTime,TotalCPU,ExitCode
