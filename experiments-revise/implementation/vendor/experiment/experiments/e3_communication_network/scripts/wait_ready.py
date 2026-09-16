from __future__ import annotations
import argparse, time
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('paths', nargs='+', type=Path)
p.add_argument('--timeout', type=float, default=180.0)
a = p.parse_args()
deadline = time.monotonic() + a.timeout
while time.monotonic() < deadline:
    if all((x.is_file() and x.stat().st_size > 0 for x in a.paths)):
        raise SystemExit(0)
    time.sleep(0.05)
missing = [str(x) for x in a.paths if not x.is_file()]
raise SystemExit('readiness timeout; missing: ' + ', '.join(missing))
