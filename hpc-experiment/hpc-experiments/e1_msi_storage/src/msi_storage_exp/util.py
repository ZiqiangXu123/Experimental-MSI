from __future__ import annotations
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True)

def point_id(point: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(point).encode('utf-8')).hexdigest()[:16]

def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.tmp.{os.getpid()}')
    with tmp.open('w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, path)

def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.tmp.{os.getpid()}')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)

def allocated_bytes(path: Path) -> int:
    st = path.stat()
    blocks = getattr(st, 'st_blocks', 0)
    return int(blocks * 512) if blocks else int(st.st_size)

def next_power_of_two(n: int) -> int:
    if n < 1:
        raise ValueError('epoch length n must be >= 1')
    return 1 << (n - 1).bit_length()

def human_bytes(value: int | float) -> str:
    x = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(x) < 1024.0 or unit == 'TiB':
            return f'{x:.3f} {unit}'
        x /= 1024.0
    return f'{x:.3f} TiB'

def command_output(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=15).strip()
    except Exception as exc:
        return f'unavailable: {type(exc).__name__}: {exc}'

def environment_inventory() -> dict[str, Any]:
    return {'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'hostname': socket.gethostname(), 'platform': platform.platform(), 'python': sys.version.replace('\n', ' '), 'python_executable': sys.executable, 'processor': platform.processor(), 'machine': platform.machine(), 'cpu_count': os.cpu_count(), 'slurm_job_id': os.environ.get('SLURM_JOB_ID'), 'slurm_array_job_id': os.environ.get('SLURM_ARRAY_JOB_ID'), 'slurm_array_task_id': os.environ.get('SLURM_ARRAY_TASK_ID'), 'slurm_cpus_per_task': os.environ.get('SLURM_CPUS_PER_TASK'), 'slurm_mem_per_node': os.environ.get('SLURM_MEM_PER_NODE'), 'slurm_tmpdir': os.environ.get('SLURM_TMPDIR'), 'git_revision': command_output(['git', 'rev-parse', 'HEAD']), 'filesystem': command_output(['df', '-T', '.']), 'ulimit': command_output(['bash', '-lc', 'ulimit -a'])}

def scratch_directory(base: str | None, prefix: str) -> tempfile.TemporaryDirectory[str]:
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=prefix, dir=base)
    for env_name in ('SLURM_TMPDIR', 'TMPDIR'):
        candidate = os.environ.get(env_name)
        if candidate and Path(candidate).is_dir():
            return tempfile.TemporaryDirectory(prefix=prefix, dir=candidate)
    return tempfile.TemporaryDirectory(prefix=prefix)

def iter_json_files(path: Path) -> Iterable[Path]:
    yield from sorted((p for p in path.rglob('*.json') if p.is_file()))

def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
