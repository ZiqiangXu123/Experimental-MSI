from __future__ import annotations
import argparse
import json
from pathlib import Path

def valid_result(path: Path, expected_id: str) -> bool:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    return obj.get('schema_version') == 1 and obj.get('point', {}).get('experiment') == 'E1' and (obj.get('point', {}).get('point_id') == expected_id) and isinstance(obj.get('checks'), dict)

def main() -> int:
    parser = argparse.ArgumentParser(description='List missing or invalid E1 Slurm array indices')
    parser.add_argument('plan', type=Path)
    parser.add_argument('raw', type=Path)
    args = parser.parse_args()
    missing: list[int] = []
    with args.plan.open('r', encoding='utf-8') as fh:
        for index, line in enumerate(fh):
            if not line.strip():
                continue
            point = json.loads(line)
            point_id = str(point['point_id'])
            if not valid_result(args.raw / f'{point_id}.json', point_id):
                missing.append(index)
    print(','.join((str(i) for i in missing)))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
