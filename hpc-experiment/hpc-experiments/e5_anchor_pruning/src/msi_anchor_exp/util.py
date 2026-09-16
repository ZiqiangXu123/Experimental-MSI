from __future__ import annotations
import csv
import gzip
import hashlib
import json
import math
import os
import platform
import random
import socket
import statistics
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

def stable_seed(*parts: object) -> int:
    h = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode('utf-8')
        h.update(len(encoded).to_bytes(4, 'big'))
        h.update(encoded)
    return int.from_bytes(h.digest()[:8], 'big')

def atomic_write_bytes(path: str | Path, data: bytes, mode: int=420) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def atomic_write_text(path: str | Path, text: str, mode: int=420) -> None:
    atomic_write_bytes(path, text.encode('utf-8'), mode=mode)

def atomic_write_json(path: str | Path, value: Any, indent: int=2) -> None:
    atomic_write_text(path, json.dumps(value, indent=indent, sort_keys=True) + '\n')

def atomic_write_json_gz(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    os.close(fd)
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as handle:
            json.dump(value, handle, sort_keys=True, separators=(',', ':'))
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)

def read_json_gz(path: str | Path) -> Any:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)

def jsonl_iter(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f'non-object at {path}:{line_number}')
            yield value

def jsonl_at(path: str | Path, index: int) -> dict[str, Any]:
    if index < 0:
        raise IndexError(index)
    for current, value in enumerate(jsonl_iter(path)):
        if current == index:
            return value
    raise IndexError(index)

def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    count = 0
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return count

def sha256_file(path: str | Path, chunk_size: int=4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def quantile_sorted(values: Sequence[float | int], q: float) -> float:
    if not values:
        return float('nan')
    if len(values) == 1:
        return float(values[0])
    position = q * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    weight = position - lower
    return float(values[lower]) * (1.0 - weight) + float(values[upper]) * weight

def summarise(values: Iterable[float | int]) -> dict[str, float | int | None]:
    ordered = sorted((float(value) for value in values))
    if not ordered:
        return {'count': 0, 'min': None, 'median': None, 'p95': None, 'p99': None, 'max': None, 'mean': None}
    return {'count': len(ordered), 'min': ordered[0], 'median': quantile_sorted(ordered, 0.5), 'p95': quantile_sorted(ordered, 0.95), 'p99': quantile_sorted(ordered, 0.99), 'max': ordered[-1], 'mean': statistics.fmean(ordered)}

def bootstrap_ci(values: Sequence[float], seed: int, *, statistic: str='median', iterations: int=2000, alpha: float=0.05) -> tuple[float, float]:
    if not values:
        return (float('nan'), float('nan'))
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    rng = random.Random(seed)
    sample_count = len(values)
    estimates: list[float] = []
    for _ in range(iterations):
        sample = [values[rng.randrange(sample_count)] for _ in range(sample_count)]
        if statistic == 'mean':
            estimates.append(statistics.fmean(sample))
        else:
            estimates.append(statistics.median(sample))
    estimates.sort()
    return (quantile_sorted(estimates, alpha / 2.0), quantile_sorted(estimates, 1.0 - alpha / 2.0))

def relative_range(values: Sequence[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return float('nan')
    median = statistics.median(finite)
    if median == 0:
        return 0.0 if max(finite) == min(finite) else float('inf')
    return (max(finite) - min(finite)) / median

def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> dict[str, float | int]:
    if len(xs) != len(ys) or len(xs) < 2:
        return {'n': len(xs), 'intercept': float('nan'), 'slope': float('nan'), 'r2': float('nan'), 'rmse': float('nan'), 'max_abs_residual': float('nan')}
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denominator = sum(((value - mean_x) ** 2 for value in xs))
    slope = 0.0 if denominator == 0 else sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))) / denominator
    intercept = mean_y - slope * mean_x
    predictions = [intercept + slope * x for x in xs]
    residuals = [actual - predicted for actual, predicted in zip(ys, predictions)]
    ss_res = sum((value * value for value in residuals))
    ss_tot = sum(((value - mean_y) ** 2 for value in ys))
    r2 = 1.0 if ss_tot == 0 and ss_res == 0 else 1.0 - ss_res / ss_tot if ss_tot else 0.0
    return {'n': len(xs), 'intercept': intercept, 'slope': slope, 'r2': r2, 'rmse': math.sqrt(ss_res / len(xs)), 'max_abs_residual': max((abs(value) for value in residuals))}

def csv_write(path: str | Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None=None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    names.append(key)
        fieldnames = names
    with target.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def flatten_dict(value: dict[str, Any], prefix: str='') -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            output.update(flatten_dict(item, name))
        elif isinstance(item, (list, tuple)):
            output[name] = json.dumps(item, separators=(',', ':'))
        else:
            output[name] = item
    return output

def system_metadata() -> dict[str, Any]:
    uname = platform.uname()
    metadata: dict[str, Any] = {'hostname': socket.gethostname(), 'platform': platform.platform(), 'system': uname.system, 'release': uname.release, 'machine': uname.machine, 'processor': platform.processor(), 'python': platform.python_version(), 'python_implementation': platform.python_implementation(), 'cpu_count': os.cpu_count(), 'pid': os.getpid(), 'cwd': os.getcwd(), 'slurm': {key: value for key, value in os.environ.items() if key.startswith('SLURM_')}}
    try:
        metadata['affinity'] = sorted(os.sched_getaffinity(0))
    except Exception:
        metadata['affinity'] = None
    try:
        cpuinfo = Path('/proc/cpuinfo').read_text(errors='replace')
        metadata['cpu_model'] = next((line.split(':', 1)[1].strip() for line in cpuinfo.splitlines() if line.lower().startswith(('model name', 'hardware'))), None)
    except OSError:
        metadata['cpu_model'] = None
    try:
        metadata['cpu_governor'] = Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor').read_text().strip()
    except OSError:
        metadata['cpu_governor'] = None
    return metadata

def pin_to_first_available_cpu() -> dict[str, Any]:
    try:
        available = sorted(os.sched_getaffinity(0))
        if not available:
            return {'attempted': True, 'success': False, 'error': 'empty affinity set'}
        selected = available[0]
        os.sched_setaffinity(0, {selected})
        return {'attempted': True, 'success': True, 'cpu': selected}
    except Exception as exc:
        return {'attempted': True, 'success': False, 'error': f'{type(exc).__name__}: {exc}'}
