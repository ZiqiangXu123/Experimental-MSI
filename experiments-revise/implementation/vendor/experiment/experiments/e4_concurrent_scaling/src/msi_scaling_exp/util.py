from __future__ import annotations
import csv, gzip, hashlib, json, math, os, platform, random, socket, statistics, tempfile, time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

def stable_seed(*parts: object) -> int:
    h = hashlib.sha256()
    for part in parts:
        b = str(part).encode()
        h.update(len(b).to_bytes(4, 'big'))
        h.update(b)
    return int.from_bytes(h.digest()[:8], 'big')

def atomic_write_bytes(path: str | Path, data: bytes, mode: int=420) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

def atomic_write_text(path: str | Path, text: str, mode: int=420) -> None:
    atomic_write_bytes(path, text.encode(), mode)

def atomic_write_json(path: str | Path, obj: Any, indent: int=2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=True) + '\n')

def atomic_write_json_gz(path: str | Path, obj: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    os.close(fd)
    try:
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as f:
            json.dump(obj, f, sort_keys=True, separators=(',', ':'))
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

def read_json(path: str | Path) -> Any:
    with open(path, encoding='utf-8') as f:
        return json.load(f)

def read_json_gz(path: str | Path) -> Any:
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return json.load(f)

def jsonl_iter(path: str | Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f'non-object at {path}:{n}')
            yield obj

def jsonl_at(path: str | Path, index: int) -> dict[str, Any]:
    for i, obj in enumerate(jsonl_iter(path)):
        if i == index:
            return obj
    raise IndexError(index)

def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    count = 0
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
                count += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return count

def sha256_file(path: str | Path, chunk_size: int=4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while (chunk := f.read(chunk_size)):
            h.update(chunk)
    return h.hexdigest()

def quantile_sorted(v: Sequence[float | int], q: float) -> float:
    if not v:
        return float('nan')
    if len(v) == 1:
        return float(v[0])
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    w = pos - lo
    return float(v[lo]) * (1 - w) + float(v[hi]) * w

def summarise(values: Iterable[float | int]) -> dict[str, float | int | None]:
    d = sorted((float(x) for x in values))
    if not d:
        return {'count': 0, 'min': None, 'median': None, 'p95': None, 'p99': None, 'max': None, 'mean': None}
    return {'count': len(d), 'min': d[0], 'median': quantile_sorted(d, 0.5), 'p95': quantile_sorted(d, 0.95), 'p99': quantile_sorted(d, 0.99), 'max': d[-1], 'mean': statistics.fmean(d)}

def bootstrap_ci(values: Sequence[float], seed: int, statistic: str='median', iterations: int=2000, alpha: float=0.05) -> tuple[float, float]:
    if not values:
        return (float('nan'), float('nan'))
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    r = random.Random(seed)
    n = len(values)
    out = []
    for _ in range(iterations):
        s = [values[r.randrange(n)] for _ in range(n)]
        out.append(statistics.fmean(s) if statistic == 'mean' else statistics.median(s))
    out.sort()
    return (quantile_sorted(out, alpha / 2), quantile_sorted(out, 1 - alpha / 2))

def jains_fairness(values: Sequence[float]) -> float:
    v = [float(x) for x in values if x >= 0]
    if not v:
        return float('nan')
    den = len(v) * sum((x * x for x in v))
    return 1.0 if den == 0 else sum(v) ** 2 / den

def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return float('nan')
    mx, my = (statistics.fmean(x), statistics.fmean(y))
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    den = math.sqrt(sum((v * v for v in dx)) * sum((v * v for v in dy)))
    return sum((a * b for a, b in zip(dx, dy))) / den if den else 0.0

def relative_range(values: Sequence[float]) -> float:
    v = [float(x) for x in values if math.isfinite(float(x))]
    if not v:
        return float('nan')
    m = statistics.median(v)
    return 0.0 if m == 0 and max(v) == min(v) else (max(v) - min(v)) / m if m else float('inf')

def precise_wait_ns(delay_ns: int) -> int:
    if delay_ns <= 0:
        return 0
    start = time.perf_counter_ns()
    target = start + int(delay_ns)
    if delay_ns > 200000:
        time.sleep((delay_ns - 100000) / 1000000000.0)
    while time.perf_counter_ns() < target:
        pass
    return time.perf_counter_ns() - start

def system_metadata() -> dict[str, Any]:
    u = platform.uname()
    m = {'hostname': socket.gethostname(), 'platform': platform.platform(), 'system': u.system, 'release': u.release, 'machine': u.machine, 'processor': platform.processor(), 'python': platform.python_version(), 'python_implementation': platform.python_implementation(), 'cpu_count': os.cpu_count(), 'pid': os.getpid(), 'cwd': os.getcwd(), 'slurm': {k: v for k, v in os.environ.items() if k.startswith('SLURM_')}}
    try:
        m['affinity'] = sorted(os.sched_getaffinity(0))
    except Exception:
        m['affinity'] = None
    try:
        text = Path('/proc/cpuinfo').read_text(errors='replace')
        m['cpu_model'] = next((l.split(':', 1)[1].strip() for l in text.splitlines() if l.lower().startswith(('model name', 'hardware'))), None)
    except OSError:
        m['cpu_model'] = None
    try:
        m['cpu_governor'] = Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor').read_text().strip()
    except OSError:
        m['cpu_governor'] = None
    return m

def csv_write(path: str | Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None=None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        seen = set()
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
    with open(target, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(fieldnames), extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

def flatten_dict(obj: dict[str, Any], prefix: str='') -> dict[str, Any]:
    out = {}
    for k, v in obj.items():
        n = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            out.update(flatten_dict(v, n))
        elif isinstance(v, (list, tuple)):
            out[n] = json.dumps(v, separators=(',', ':'))
        else:
            out[n] = v
    return out
