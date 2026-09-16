#!/usr/bin/env bash
set -euo pipefail
JOB_ID=${1:?usage: job_accounting.sh JOB_ID}
sacct -j "$JOB_ID" --units=M \
  --format=JobID,JobName%24,State,Elapsed,AllocCPUS,ReqMem,MaxRSS,ExitCode
