from __future__ import annotations
import argparse, json, shlex
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('case', type=Path)
a = p.parse_args()
o = json.loads(a.case.read_text())
fields = {'CASE_ID': str(o['case_id']), 'KIND': str(o['kind']), 'DEPLOYMENT': str(o.get('deployment', 'coalesced')), 'MEASURED': str(int(o.get('measured_queries', 1))), 'WARMUP': str(int(o.get('warmup_queries', 0)))}
for k, v in fields.items():
    print(f'{k}={shlex.quote(v)}')
