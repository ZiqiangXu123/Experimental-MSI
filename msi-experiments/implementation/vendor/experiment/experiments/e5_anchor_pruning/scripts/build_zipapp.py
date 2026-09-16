from __future__ import annotations
import os
import sys
import zipapp
from pathlib import Path
root = Path(__file__).resolve().parents[1]
target = root / 'exp05_anchor.pyz'
if target.exists():
    target.unlink()
zipapp.create_archive(root / 'src', target=target, interpreter='/usr/bin/env python3', compressed=True)
os.chmod(target, 493)
print(target)
