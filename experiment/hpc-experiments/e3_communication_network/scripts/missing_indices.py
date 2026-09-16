from __future__ import annotations
import argparse
import gzip
import json
from pathlib import Path
from typing import Any

def load(path: Path) -> Any:
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        return json.load(fh)

def valid_result(path: Path, expected_id: str) -> bool:
    try:
        obj = load(path)
    except (OSError, EOFError, json.JSONDecodeError, ValueError):
        return False
    return obj.get('schema_version') == 1 and obj.get('experiment') == 'E3' and (obj.get('case', {}).get('case_id') == expected_id) and isinstance(obj.get('checks'), dict) and bool(obj['checks']) and all((bool(value) for value in obj['checks'].values()))

def main() -> int:
    parser = argparse.ArgumentParser(description='List missing or invalid E3 Slurm block indices')
    parser.add_argument('plan', type=Path)
    parser.add_argument('raw', type=Path)
    args = parser.parse_args()
    missing: list[int] = []
    with args.plan.open('r', encoding='utf-8') as fh:
        for index, line in enumerate(fh):
            if not line.strip():
                continue
            block = json.loads(line)
            if any((not valid_result(args.raw / f"{case['case_id']}.json.gz", str(case['case_id'])) for case in block['cases'])):
                missing.append(index)
    print(','.join((str(index) for index in missing)))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
