from __future__ import annotations
import argparse, shutil, tempfile, zipapp
from pathlib import Path

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    source = Path(a.source).resolve()
    output = Path(a.output).resolve()
    with tempfile.TemporaryDirectory(prefix='e6-zipapp-') as td:
        stage = Path(td) / 'app'
        shutil.copytree(source, stage)
        zipapp.create_archive(stage, target=output, interpreter='/usr/bin/env python3', compressed=True)
    output.chmod(493)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
