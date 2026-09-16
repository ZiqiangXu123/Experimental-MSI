#!/usr/bin/env bash
set -euo pipefail
MSI_ENTRY_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10 or newer is required; python3 was not found." >&2
    exit 2
fi
python3 -c 'import sys; sys.exit("Python 3.10 or newer is required.") if sys.version_info < (3, 10) else None'
cd "$MSI_ENTRY_ROOT"
exec python3 -m msi_supplement "$@"
