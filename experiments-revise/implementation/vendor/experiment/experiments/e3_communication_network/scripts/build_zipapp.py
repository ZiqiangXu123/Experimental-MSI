from __future__ import annotations
import argparse
import os
import shutil
import tempfile
import zipapp
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='e3-zipapp-') as tmp:
        staging = Path(tmp) / 'src'
        shutil.copytree(args.source, staging, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        zipapp.create_archive(staging, target=args.output, interpreter='/usr/bin/env python3', compressed=True)
    os.chmod(args.output, 493)
    print(args.output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
