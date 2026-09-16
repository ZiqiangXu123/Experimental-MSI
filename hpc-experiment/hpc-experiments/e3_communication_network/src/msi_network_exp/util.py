from __future__ import annotations
import gzip
import hashlib
import json
import math
import os
import platform
import random
import resource
import socket
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

def next_power_of_two(n: int) -> int:
    if n < 1:
        raise ValueError('n must be positive')
    return 1 << (n - 1).bit_length()

def is_power_of_two(n: int) -> bool:
    return n > 0 and n & n - 1 == 0

def stable_seed(*parts: object) -> int:
    h = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode('utf-8')
        h.update(len(encoded).to_bytes(4, 'big'))
        h.update(encoded)
    return int.from_bytes(h.digest()[:8], 'big')

def percentile_sorted(ordered: Sequence[float], p: float) -> float:
    if not ordered:
        raise ValueError('percentile requires at least one value')
    if not 0 <= p <= 100:
        raise ValueError('percentile p must be in [0,100]')
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p / 100.0
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(ordered[lo])
    weight = rank - lo
    return float(ordered[lo]) * (1.0 - weight) + float(ordered[hi]) * weight

def percentile(values: Sequence[float | int], p: float) -> float:
    ordered = sorted((float(x) for x in values))
    return percentile_sorted(ordered, p)

def summarise(values: Sequence[float | int]) -> dict[str, float | int]:
    if not values:
        return {'n': 0}
    ordered = sorted((float(v) for v in values))
    return {'n': len(ordered), 'min': ordered[0], 'median': percentile_sorted(ordered, 50), 'p95': percentile_sorted(ordered, 95), 'p99': percentile_sorted(ordered, 99), 'mean': statistics.fmean(ordered), 'max': ordered[-1]}

def bootstrap_ci(values: Sequence[float], statistic: str='mean', confidence: float=0.95, resamples: int=5000, seed: int=0) -> tuple[float, float]:
    if not values:
        return (math.nan, math.nan)
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    rng = random.Random(seed)
    n = len(values)
    stats: list[float] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        if statistic == 'median':
            stats.append(float(statistics.median(sample)))
        elif statistic == 'mean':
            stats.append(float(statistics.fmean(sample)))
        else:
            raise ValueError(f'unsupported bootstrap statistic: {statistic}')
    alpha = (1.0 - confidence) * 50.0
    return (percentile(stats, alpha), percentile(stats, 100.0 - alpha))

def paired_bootstrap_ci(xs: Sequence[float], ys: Sequence[float], resamples: int=5000, seed: int=0) -> tuple[float, float, float]:
    if len(xs) != len(ys) or not xs:
        return (math.nan, math.nan, math.nan)
    diffs = [float(x) - float(y) for x, y in zip(xs, ys)]
    point = statistics.fmean(diffs)
    lo, hi = bootstrap_ci(diffs, statistic='mean', resamples=resamples, seed=seed)
    return (point, lo, hi)

def cliffs_delta(xs: Sequence[float], ys: Sequence[float]) -> float:
    if not xs or not ys:
        return math.nan
    greater = 0
    lower = 0
    for x in xs:
        for y in ys:
            if x > y:
                greater += 1
            elif x < y:
                lower += 1
    return (greater - lower) / (len(xs) * len(ys))

def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

def atomic_write_json(path: Path, obj: Any, compress: bool=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = '.tmp.gz' if compress else '.tmp'
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix=suffix, dir=path.parent)
    os.close(fd)
    try:
        if compress:
            with gzip.open(tmp_name, 'wt', encoding='utf-8', compresslevel=6) as fh:
                json.dump(obj, fh, sort_keys=True, separators=(',', ':'))
        else:
            with open(tmp_name, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh, sort_keys=True, indent=2)
                fh.write('\n')
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

def read_json(path: Path) -> Any:
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding='utf-8'))

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def get_rss_kib() -> int | None:
    status = Path('/proc/self/status')
    if status.exists():
        for line in status.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('VmRSS:'):
                fields = line.split()
                if len(fields) >= 2:
                    return int(fields[1])
    usage = resource.getrusage(resource.RUSAGE_SELF)
    value = int(usage.ru_maxrss)
    if platform.system() == 'Darwin':
        return value // 1024
    return value

def get_peak_rss_kib() -> int | None:
    status = Path('/proc/self/status')
    if status.exists():
        for line in status.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('VmHWM:'):
                fields = line.split()
                if len(fields) >= 2:
                    return int(fields[1])
    usage = resource.getrusage(resource.RUSAGE_SELF)
    value = int(usage.ru_maxrss)
    if platform.system() == 'Darwin':
        return value // 1024
    return value

def pin_to_cpu_offset(offset: int=0) -> dict[str, Any]:
    result: dict[str, Any] = {'supported': False, 'pinned': False, 'requested_offset': int(offset)}
    if os.environ.get('E3_DISABLE_PINNING') == '1':
        result['reason'] = 'disabled by E3_DISABLE_PINNING'
        return result
    if not hasattr(os, 'sched_getaffinity') or not hasattr(os, 'sched_setaffinity'):
        result['reason'] = 'sched affinity API unavailable'
        return result
    try:
        allowed = sorted(os.sched_getaffinity(0))
        result.update({'supported': True, 'allowed_cpus': allowed})
        if not allowed:
            result['reason'] = 'empty affinity set'
            return result
        selected = allowed[int(offset) % len(allowed)]
        os.sched_setaffinity(0, {selected})
        result.update({'pinned': True, 'selected_cpu': selected})
        return result
    except OSError as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
        return result

def pin_to_single_cpu() -> dict[str, Any]:
    return pin_to_cpu_offset(0)

def read_cpu_governor(cpu: int | None) -> str | None:
    if cpu is None:
        return None
    path = Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor')
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError:
        return None

def _cpu_model() -> str | None:
    cpuinfo = Path('/proc/cpuinfo')
    try:
        for line in cpuinfo.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.lower().startswith(('model name', 'hardware')) and ':' in line:
                return line.split(':', 1)[1].strip()
    except OSError:
        pass
    value = platform.processor().strip()
    return value or None

def system_metadata() -> dict[str, Any]:
    return {'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'hostname': socket.gethostname(), 'platform': platform.platform(), 'machine': platform.machine(), 'cpu_model': _cpu_model(), 'logical_cpu_count': os.cpu_count(), 'python': platform.python_version(), 'python_implementation': platform.python_implementation(), 'pid': os.getpid(), 'experiment_topology': os.environ.get('E3_RUN_TOPOLOGY', 'local-portable'), 'slurm': {key: os.environ.get(key) for key in ('SLURM_JOB_ID', 'SLURM_ARRAY_JOB_ID', 'SLURM_ARRAY_TASK_ID', 'SLURM_JOB_NAME', 'SLURM_CPUS_PER_TASK', 'SLURM_JOB_CPUS_PER_NODE', 'SLURM_NODELIST', 'SLURM_MEM_PER_NODE', 'SLURM_MEM_PER_CPU', 'SLURM_JOB_PARTITION', 'SLURM_JOB_ACCOUNT', 'SLURM_QOS', 'SLURM_CPU_BIND', 'SLURM_CPU_BIND_LIST')}}

def product_dict(grid: dict[str, Sequence[Any]]) -> Iterable[dict[str, Any]]:
    keys = list(grid)
    if not keys:
        yield {}
        return
    rows: list[dict[str, Any]] = [{}]
    for key in keys:
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            for value in grid[key]:
                nxt = dict(row)
                nxt[key] = value
                new_rows.append(nxt)
        rows = new_rows
    yield from rows
