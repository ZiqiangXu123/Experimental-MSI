from __future__ import annotations
import csv
import gzip
import hashlib
import json
import math
import os
import platform
import resource
import socket
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
MASK64 = (1 << 64) - 1

class SplitMix64:

    def __init__(self, seed: int):
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = self.state + 11400714819323198485 & MASK64
        z = self.state
        z = (z ^ z >> 30) * 13787848793156543929 & MASK64
        z = (z ^ z >> 27) * 10723151780598845931 & MASK64
        return (z ^ z >> 31) & MASK64

    def random(self) -> float:
        return (self.next_u64() >> 11) * (1.0 / (1 << 53))

    def randbelow(self, n: int) -> int:
        if n <= 0:
            raise ValueError('n must be positive')
        return self.next_u64() % n

    def choice(self, values: Sequence[Any]) -> Any:
        return values[self.randbelow(len(values))]

def next_power_of_two(value: int) -> int:
    if value < 1:
        raise ValueError('value must be >= 1')
    return 1 << (value - 1).bit_length()

def is_power_of_two(value: int) -> bool:
    return value > 0 and value & value - 1 == 0

def percentile(values: Sequence[float | int], q: float) -> float:
    if not values:
        return float('nan')
    if not 0 <= q <= 1:
        raise ValueError('q must be between zero and one')
    ordered = sorted((float(v) for v in values))
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)

def median(values: Sequence[float | int]) -> float:
    return float(statistics.median(values)) if values else float('nan')

def bootstrap_median_ci(values: Sequence[float | int], seed: int, samples: int=2000) -> tuple[float, float]:
    if not values:
        return (float('nan'), float('nan'))
    if len(values) == 1:
        v = float(values[0])
        return (v, v)
    rng = SplitMix64(seed)
    vals = [float(v) for v in values]
    medians: list[float] = []
    for _ in range(samples):
        draw = [vals[rng.randbelow(len(vals))] for _ in vals]
        medians.append(float(statistics.median(draw)))
    return (percentile(medians, 0.025), percentile(medians, 0.975))

def upper_zero_event_bound(trials: int, alpha: float=0.05) -> float:
    if trials <= 0:
        return float('nan')
    return 1.0 - alpha ** (1.0 / trials)

def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode('utf-8'))

def atomic_write_json(path: Path, obj: Any, *, indent: int=2) -> None:
    atomic_write_text(path, json.dumps(obj, sort_keys=True, indent=indent, ensure_ascii=False) + '\n')

def atomic_write_gzip_json(path: Path, obj: Any) -> None:
    payload = json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as gz:
                gz.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

def read_gzip_json(path: Path) -> Any:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)

def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: Sequence[str] | None=None) -> None:
    rows_list = list(rows)
    if fieldnames is None:
        names: list[str] = []
        seen: set[str] = set()
        for row in rows_list:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    names.append(key)
        fieldnames = names
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction='ignore')
            writer.writeheader()
            for row in rows_list:
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'invalid JSONL at {path}:{line_no}: {exc}') from exc
            if not isinstance(value, dict):
                raise ValueError(f'JSONL row must be an object at {path}:{line_no}')
            out.append(value)
    return out

def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = ''.join((json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n' for row in rows))
    atomic_write_text(path, text)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def write_manifest(directory: Path, exclude: set[str] | None=None) -> Path:
    exclude = exclude or {'manifest.sha256'}
    rows: list[str] = []
    for path in sorted((p for p in directory.rglob('*') if p.is_file())):
        rel = path.relative_to(directory).as_posix()
        if rel in exclude:
            continue
        rows.append(f'{sha256_file(path)}  {rel}\n')
    manifest = directory / 'manifest.sha256'
    atomic_write_text(manifest, ''.join(rows))
    return manifest

def validate_manifest(directory: Path) -> tuple[bool, list[str]]:
    manifest = directory / 'manifest.sha256'
    if not manifest.exists():
        return (False, ['manifest.sha256 is missing'])
    errors: list[str] = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            digest, rel = line.split('  ', 1)
        except ValueError:
            errors.append(f'malformed manifest row: {line}')
            continue
        path = directory / rel
        if not path.is_file():
            errors.append(f'missing: {rel}')
        elif sha256_file(path) != digest:
            errors.append(f'digest mismatch: {rel}')
    return (not errors, errors)

def get_peak_rss_kib() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == 'darwin':
        value //= 1024
    return value

def system_info() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {'hostname': socket.gethostname(), 'platform': platform.platform(), 'machine': platform.machine(), 'processor': platform.processor(), 'python': sys.version, 'python_executable': sys.executable, 'pid': os.getpid(), 'cwd': os.getcwd(), 'maxrss_raw': usage.ru_maxrss, 'peak_rss_kib': get_peak_rss_kib(), 'slurm': {k: os.environ.get(k) for k in ('SLURM_JOB_ID', 'SLURM_ARRAY_JOB_ID', 'SLURM_ARRAY_TASK_ID', 'SLURM_CPUS_PER_TASK', 'SLURM_JOB_NODELIST', 'SLURM_JOB_PARTITION', 'SLURM_JOB_ACCOUNT', 'SLURM_JOB_QOS') if os.environ.get(k) is not None}}

def chunks(values: Sequence[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        raise ValueError('chunk size must be positive')
    for i in range(0, len(values), size):
        yield list(values[i:i + size])
