from __future__ import annotations
import argparse
import zipapp
from pathlib import Path

def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=root / 'src')
    parser.add_argument('--output', type=Path, default=root / 'exp02_query.pyz')
    args = parser.parse_args()
    zipapp.create_archive(args.source, target=args.output, interpreter='/usr/bin/env python3', compressed=True)
    args.output.chmod(493)
    print(args.output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
