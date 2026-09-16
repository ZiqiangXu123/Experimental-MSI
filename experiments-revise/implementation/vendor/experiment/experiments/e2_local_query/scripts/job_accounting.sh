#!/usr/bin/env bash
set -euo pipefail
JOB_ID=${1:?usage: job_accounting.sh JOB_ID}
sacct -j "$JOB_ID" --units=M \
  --format=JobID,JobName%28,State,Elapsed,AllocCPUS,ReqMem,MaxRSS,AveCPU,TotalCPU,ExitCode
if command -v seff >/dev/null 2>&1; then
  echo '--- seff ---'
  seff "$JOB_ID" || true
fi
