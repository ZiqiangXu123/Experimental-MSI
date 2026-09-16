from __future__ import annotations
import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser(description='Build the dependency-free E4 Python zipapp.')
    parser.add_argument('--source', default='src')
    parser.add_argument('--output', default='exp04_scaling.pyz')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not (source / '__main__.py').is_file():
        raise SystemExit('source root lacks __main__.py')
    with tempfile.TemporaryDirectory(prefix='e4-zipapp-') as tmp:
        staging = Path(tmp) / 'app'
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        zipapp.create_archive(staging, target=output, interpreter='/usr/bin/env python3', compressed=True)
    output.chmod(493)
    print(output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
