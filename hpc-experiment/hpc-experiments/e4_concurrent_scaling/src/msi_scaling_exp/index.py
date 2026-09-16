from __future__ import annotations
import concurrent.futures, fcntl, hashlib, json, mmap, os, struct, time, zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from .merkle import fixture_root, hot_fixture
from .util import atomic_write_json, sha256_file
MAGIC = b'MSIE4IDX'
HEADER_BYTES = 4096
RECORD = struct.Struct('>32sQQIHHQ')
RECORD_BYTES = RECORD.size
FLAG_HOT = 1
CODEC_RAW = 1

@dataclass(frozen=True)
class DatasetSpec:
    shard_count: int
    historical_epochs: int
    epoch_length: int = 512
    block_bytes: int = 2048
    seed: int = 44012026
    hotset_size: int = 256

    @property
    def total_records(self):
        return self.shard_count * self.historical_epochs

    @property
    def dataset_id(self):
        return 'idx-' + hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]

    def as_dict(self):
        return {'shard_count': self.shard_count, 'historical_epochs': self.historical_epochs, 'epoch_length': self.epoch_length, 'block_bytes': self.block_bytes, 'seed': self.seed, 'hotset_size': min(self.hotset_size, self.total_records), 'record_bytes': RECORD_BYTES}

    @classmethod
    def from_dict(cls, o):
        return cls(int(o['shard_count']), int(o['historical_epochs']), int(o.get('epoch_length', 512)), int(o.get('block_bytes', 2048)), int(o.get('fixture_seed', o.get('dataset_seed', o.get('seed', 44012026)))), int(o.get('hotset_size', 256)))

def hot_indices(spec: DatasetSpec):
    total = spec.total_records
    c = min(max(1, spec.hotset_size), total)
    if c == 1:
        return (total - 1,)
    v = {round(i * (total - 1) / (c - 1)) for i in range(c)}
    x = total - 1
    while len(v) < c and x >= 0:
        v.add(x)
        x -= 1
    return tuple(sorted(v))

def index_to_key(spec: DatasetSpec, idx: int):
    return divmod(idx, spec.historical_epochs)

def key_to_index(spec: DatasetSpec, s: int, t: int):
    if not 0 <= s < spec.shard_count or not 0 <= t < spec.historical_epochs:
        raise IndexError((s, t))
    return s * spec.historical_epochs + t

def refs_for(spec: DatasetSpec, s: int, t: int):
    b = struct.pack('>QQQ', s, t, spec.seed & (1 << 64) - 1)
    return tuple((int.from_bytes(hashlib.sha256(tag + b).digest()[:8], 'big') for tag in (b'PAYLOAD-REF\x00', b'AUX-REF\x00', b'RECORD-TAG\x00')))

def record_for(spec: DatasetSpec, idx: int, hot: set[int]):
    s, t = index_to_key(spec, idx)
    flags = 0
    if idx in hot:
        root = hot_fixture(s, t, spec.epoch_length, spec.block_bytes, spec.seed)[3]
        flags = FLAG_HOT
    else:
        root = fixture_root(s, t, spec.epoch_length, spec.block_bytes, spec.seed)
    p, a, tag = refs_for(spec, s, t)
    return RECORD.pack(root, p, a, spec.epoch_length, flags, CODEC_RAW, tag)

def _chunk(args):
    d, start, end, hot = args
    spec = DatasetSpec.from_dict(d)
    hs = set(hot)
    out = bytearray((end - start) * RECORD_BYTES)
    o = 0
    for idx in range(start, end):
        out[o:o + RECORD_BYTES] = record_for(spec, idx, hs)
        o += RECORD_BYTES
    return (start, bytes(out))

def _header(spec, hot):
    meta = {'schema_version': 1, 'dataset_id': spec.dataset_id, 'created_unix_ns': time.time_ns(), 'spec': spec.as_dict(), 'total_records': spec.total_records, 'record_bytes': RECORD_BYTES, 'header_bytes': HEADER_BYTES, 'hot_indices': list(hot), 'hot_fixture_semantics': 'Leaf-mode trials sample only deterministic hot-set records; other records use a valid position-0 authenticated path fixture.'}
    raw = json.dumps(meta, sort_keys=True, separators=(',', ':')).encode()
    prefix = MAGIC + struct.pack('>II', len(raw), zlib.crc32(raw) & 4294967295)
    if len(prefix) + len(raw) > HEADER_BYTES:
        raise ValueError('header too large')
    return prefix + raw + bytes(HEADER_BYTES - len(prefix) - len(raw))

def dataset_paths(directory: str | Path, spec: DatasetSpec):
    b = Path(directory) / spec.dataset_id
    return (b.with_suffix('.msiidx'), b.with_suffix('.json'))

def build_dataset(directory: str | Path, spec: DatasetSpec, workers: int=1, chunk_records: int=4096, force: bool=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data, meta = dataset_paths(directory, spec)
    lock = data.with_suffix(data.suffix + '.lock')
    with open(lock, 'a+b') as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        if data.exists() and meta.exists() and (not force):
            try:
                return validate_dataset(data, spec)
            except Exception:
                pass
        hot = hot_indices(spec)
        tmp = data.with_suffix(data.suffix + f'.tmp.{os.getpid()}')
        start_ns = time.perf_counter_ns()
        try:
            with open(tmp, 'wb') as f:
                f.write(_header(spec, hot))
                tasks = [(spec.as_dict(), s, min(spec.total_records, s + chunk_records), hot) for s in range(0, spec.total_records, chunk_records)]
                if workers > 1 and len(tasks) > 1:
                    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
                        for expected, (actual, blob) in zip((t[1] for t in tasks), ex.map(_chunk, tasks, chunksize=1)):
                            if expected != actual:
                                raise RuntimeError('chunk order')
                            f.write(blob)
                else:
                    hs = set(hot)
                    for _, s, e, _ in tasks:
                        f.write(b''.join((record_for(spec, i, hs) for i in range(s, e))))
                f.flush()
                os.fsync(f.fileno())
            expected = HEADER_BYTES + spec.total_records * RECORD_BYTES
            if tmp.stat().st_size != expected:
                raise RuntimeError(f'size {tmp.stat().st_size}!={expected}')
            os.replace(tmp, data)
            m = {'schema_version': 1, 'dataset_id': spec.dataset_id, 'path': str(data), 'sha256': sha256_file(data), 'size_bytes': data.stat().st_size, 'build_elapsed_ns': time.perf_counter_ns() - start_ns, 'workers': workers, 'spec': spec.as_dict(), 'hot_indices': list(hot)}
            atomic_write_json(meta, m)
            return validate_dataset(data, spec)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

def read_header(path):
    with open(path, 'rb') as f:
        r = f.read(HEADER_BYTES)
    if len(r) != HEADER_BYTES or r[:8] != MAGIC:
        raise ValueError('bad index')
    n, crc = struct.unpack_from('>II', r, 8)
    b = r[16:16 + n]
    if zlib.crc32(b) & 4294967295 != crc:
        raise ValueError('header crc')
    return json.loads(b)

def validate_dataset(path, expected_spec=None):
    p = Path(path)
    h = read_header(p)
    spec = DatasetSpec.from_dict(h['spec'])
    if expected_spec is not None and spec != expected_spec:
        raise ValueError('spec mismatch')
    if p.stat().st_size != HEADER_BYTES + spec.total_records * RECORD_BYTES:
        raise ValueError('size mismatch')
    hot = set(h['hot_indices'])
    with IndexReader(p) as r:
        for idx in {0, spec.total_records - 1, *list(hot)[:3]}:
            s, t = index_to_key(spec, idx)
            rec, _ = r.lookup(s, t)
            if rec['raw'] != record_for(spec, idx, hot):
                raise ValueError(f'record {idx}')
    return {'status': 'PASS', 'path': str(p), 'dataset_id': spec.dataset_id, 'size_bytes': p.stat().st_size, 'total_records': spec.total_records, 'spec': spec.as_dict(), 'hotset_size': len(hot), 'header': h}

class IndexReader:

    def __init__(self, path):
        self.path = Path(path)
        self.header = read_header(path)
        self.spec = DatasetSpec.from_dict(self.header['spec'])
        self.f = open(path, 'rb', buffering=0)
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            self.mm.madvise(mmap.MADV_RANDOM)
        except Exception:
            pass

    def lookup(self, s, t):
        st = time.perf_counter_ns()
        idx = key_to_index(self.spec, s, t)
        o = HEADER_BYTES + idx * RECORD_BYTES
        root, p, a, n, flags, codec, tag = RECORD.unpack_from(self.mm, o)
        elapsed = time.perf_counter_ns() - st
        raw = self.mm[o:o + RECORD_BYTES]
        return ({'index': idx, 'root': root, 'payload_ref': p, 'aux_ref': a, 'n': n, 'flags': flags, 'codec': codec, 'tag': tag, 'hot': bool(flags & FLAG_HOT), 'raw': raw}, elapsed)

    def touch(self, indices: Iterable[int]):
        x = 0
        for i in indices:
            if 0 <= i < self.spec.total_records:
                x ^= self.mm[HEADER_BYTES + i * RECORD_BYTES]
        return x

    def close(self):
        self.mm.close()
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
