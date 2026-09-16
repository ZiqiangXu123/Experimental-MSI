from __future__ import annotations
import argparse
import zipapp
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'exp01_storage.pyz')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(args.source, target=args.output, interpreter='/usr/bin/env python3', compressed=True)
    args.output.chmod(493)
    print(args.output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
