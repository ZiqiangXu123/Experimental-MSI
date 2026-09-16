from __future__ import annotations
import argparse
import os
import zipapp
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='src')
    parser.add_argument('--output', default='exp07_security_availability.pyz')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not (source / '__main__.py').is_file():
        raise SystemExit(f"missing {source / '__main__.py'}")
    output.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(source, target=output, interpreter='/usr/bin/env python3', compressed=True)
    os.chmod(output, 493)
    print(output)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
