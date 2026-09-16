#!/usr/bin/env bash
SBATCH_SITE_OPTIONS=()
[[ -n "${SBATCH_ACCOUNT:-}" ]] && SBATCH_SITE_OPTIONS+=(--account="$SBATCH_ACCOUNT")
[[ -n "${SBATCH_PARTITION:-}" ]] && SBATCH_SITE_OPTIONS+=(--partition="$SBATCH_PARTITION")
[[ -n "${SBATCH_QOS:-}" ]] && SBATCH_SITE_OPTIONS+=(--qos="$SBATCH_QOS")
[[ -n "${SBATCH_RESERVATION:-}" ]] && SBATCH_SITE_OPTIONS+=(--reservation="$SBATCH_RESERVATION")
true
