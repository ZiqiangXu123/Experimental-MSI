from __future__ import annotations
import json
import mmap
import os
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from .accumulator import DIGEST_BYTES, AccumulatorProof, FrontierAccumulator, acc_final_root, bag_peaks, deterministic_statement, peak_layout, statement_leaf_hash, acc_node_hash
from .util import atomic_write_json, sha256_file, stable_seed

@dataclass(frozen=True)
class DatasetSpec:
    max_prefix: int
    shard: int
    statement_seed: int

    def __post_init__(self) -> None:
        if self.max_prefix <= 0 or self.max_prefix & self.max_prefix - 1:
            raise ValueError('max_prefix must be a positive power of two')
        if self.max_prefix > 1 << 24:
            raise ValueError('max_prefix exceeds the artifact safety limit')
        if not 0 <= self.shard <= 4294967295:
            raise ValueError('shard is outside uint32 range')

    @property
    def depth(self) -> int:
        return self.max_prefix.bit_length() - 1

    @property
    def dataset_id(self) -> str:
        return f'acc-s{self.shard}-n{self.max_prefix}-seed{self.statement_seed:016x}'

    @property
    def logical_tree_bytes(self) -> int:
        return (2 * self.max_prefix - 1) * DIGEST_BYTES

    def as_dict(self) -> dict[str, int | str]:
        return {'dataset_id': self.dataset_id, 'max_prefix': self.max_prefix, 'shard': self.shard, 'statement_seed': self.statement_seed, 'depth': self.depth, 'logical_tree_bytes': self.logical_tree_bytes}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'DatasetSpec':
        return cls(max_prefix=int(value['max_prefix']), shard=int(value.get('shard', 1)), statement_seed=int(value['statement_seed']))

def dataset_path(base: str | Path, spec: DatasetSpec) -> Path:
    return Path(base) / spec.dataset_id

def level_path(directory: str | Path, level: int) -> Path:
    return Path(directory) / f'level_{level:02d}.bin'

def _write_leaf_level(path: Path, spec: DatasetSpec, chunk_records: int=8192) -> None:
    with path.open('wb', buffering=4 * 1024 * 1024) as handle:
        buffer = bytearray()
        for position in range(1, spec.max_prefix + 1):
            statement = deterministic_statement(spec.statement_seed, spec.shard, position)
            buffer.extend(statement_leaf_hash(statement))
            if position % chunk_records == 0:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)
        handle.flush()
        os.fsync(handle.fileno())

def _write_parent_level(previous: Path, target: Path, child_count: int) -> None:
    if child_count % 2:
        raise ValueError('perfect-tree level contains an odd number of children')
    with previous.open('rb', buffering=4 * 1024 * 1024) as source, target.open('wb', buffering=4 * 1024 * 1024) as output:
        remaining = child_count
        while remaining:
            pair = source.read(2 * DIGEST_BYTES)
            if len(pair) != 2 * DIGEST_BYTES:
                raise IOError('source level ended before the expected child count')
            output.write(acc_node_hash(pair[:DIGEST_BYTES], pair[DIGEST_BYTES:]))
            remaining -= 2
        if source.read(1):
            raise IOError('source level contains unexpected trailing bytes')
        output.flush()
        os.fsync(output.fileno())

def _read_digest(path: Path, index: int) -> bytes:
    with path.open('rb') as handle:
        handle.seek(index * DIGEST_BYTES)
        value = handle.read(DIGEST_BYTES)
    if len(value) != DIGEST_BYTES:
        raise IOError(f'digest index {index} is outside {path}')
    return value

def build_dataset(base: str | Path, spec: DatasetSpec, *, force: bool=False) -> dict[str, Any]:
    target = dataset_path(base, spec)
    if target.exists() and (not force):
        validation = validate_dataset(target, spec, verify_hashes=False)
        if validation['status'] == 'PASS':
            return validation
    Path(base).mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f'.{spec.dataset_id}.', dir=str(Path(base))))
    started = time.time()
    try:
        _write_leaf_level(level_path(temporary, 0), spec)
        child_count = spec.max_prefix
        for level in range(1, spec.depth + 1):
            _write_parent_level(level_path(temporary, level - 1), level_path(temporary, level), child_count)
            child_count //= 2
        prefix_roots: dict[str, str] = {}
        for depth in range(spec.depth + 1):
            prefix = 1 << depth
            peak = _read_digest(level_path(temporary, depth), 0)
            prefix_roots[str(prefix)] = acc_final_root(prefix, peak).hex()
        files: dict[str, dict[str, Any]] = {}
        for level in range(spec.depth + 1):
            path = level_path(temporary, level)
            files[path.name] = {'bytes': path.stat().st_size, 'sha256': sha256_file(path), 'nodes': spec.max_prefix >> level}
        metadata = {'schema_version': 1, 'experiment': 'E5', 'dataset': spec.as_dict(), 'root_construction': 'append-only Merkle forest; power-of-two prefix uses one perfect peak', 'files': files, 'prefix_roots': prefix_roots, 'build_wall_seconds': time.time() - started}
        atomic_write_json(temporary / 'metadata.json', metadata)
        if target.exists():
            shutil.rmtree(target)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return validate_dataset(target, spec, verify_hashes=False)

def validate_dataset(directory: str | Path, spec: DatasetSpec | None=None, *, verify_hashes: bool=False) -> dict[str, Any]:
    directory = Path(directory)
    metadata_path = directory / 'metadata.json'
    errors: list[str] = []
    if not metadata_path.is_file():
        return {'status': 'FAIL', 'directory': str(directory), 'errors': ['metadata.json is missing']}
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        recorded = DatasetSpec.from_dict(metadata['dataset'])
    except Exception as exc:
        return {'status': 'FAIL', 'directory': str(directory), 'errors': [f'metadata parse failed: {type(exc).__name__}: {exc}']}
    if spec is not None and recorded != spec:
        errors.append(f'recorded spec {recorded} differs from requested spec {spec}')
    for level in range(recorded.depth + 1):
        path = level_path(directory, level)
        expected = (recorded.max_prefix >> level) * DIGEST_BYTES
        if not path.is_file():
            errors.append(f'{path.name} is missing')
            continue
        actual = path.stat().st_size
        if actual != expected:
            errors.append(f'{path.name}: {actual} bytes, expected {expected}')
        if verify_hashes:
            recorded_hash = metadata.get('files', {}).get(path.name, {}).get('sha256')
            actual_hash = sha256_file(path)
            if recorded_hash != actual_hash:
                errors.append(f'{path.name}: SHA-256 mismatch')
    try:
        top = _read_digest(level_path(directory, recorded.depth), 0)
        computed = acc_final_root(recorded.max_prefix, top).hex()
        expected_root = metadata['prefix_roots'][str(recorded.max_prefix)]
        if computed != expected_root:
            errors.append('maximum-prefix root mismatch')
    except Exception as exc:
        errors.append(f'root validation failed: {type(exc).__name__}: {exc}')
    return {'status': 'PASS' if not errors else 'FAIL', 'directory': str(directory), 'dataset': recorded.as_dict(), 'logical_tree_bytes': recorded.logical_tree_bytes, 'allocated_bytes': sum((level_path(directory, level).stat().st_size for level in range(recorded.depth + 1) if level_path(directory, level).is_file())), 'verify_hashes': verify_hashes, 'errors': errors}

class MappedAccumulatorDataset:

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        metadata = json.loads((self.directory / 'metadata.json').read_text(encoding='utf-8'))
        self.metadata = metadata
        self.spec = DatasetSpec.from_dict(metadata['dataset'])
        self._files: list[Any] = []
        self._maps: list[mmap.mmap] = []
        for level in range(self.spec.depth + 1):
            handle = level_path(self.directory, level).open('rb')
            self._files.append(handle)
            self._maps.append(mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ))

    def close(self) -> None:
        for mapped in self._maps:
            mapped.close()
        for handle in self._files:
            handle.close()
        self._maps.clear()
        self._files.clear()

    def __enter__(self) -> 'MappedAccumulatorDataset':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def node(self, level: int, index: int) -> bytes:
        if not 0 <= level <= self.spec.depth:
            raise IndexError(level)
        count = self.spec.max_prefix >> level
        if not 0 <= index < count:
            raise IndexError(index)
        offset = index * DIGEST_BYTES
        return self._maps[level][offset:offset + DIGEST_BYTES]

    def statement(self, position: int):
        if not 1 <= position <= self.spec.max_prefix:
            raise IndexError(position)
        return deterministic_statement(self.spec.statement_seed, self.spec.shard, position)

    def leaf(self, position: int) -> bytes:
        if not 1 <= position <= self.spec.max_prefix:
            raise IndexError(position)
        return self.node(0, position - 1)

    def power_of_two_root(self, prefix_count: int) -> bytes:
        if prefix_count <= 0 or prefix_count & prefix_count - 1:
            raise ValueError('prefix_count must be a positive power of two')
        if prefix_count > self.spec.max_prefix:
            raise ValueError('prefix_count exceeds the dataset')
        level = prefix_count.bit_length() - 1
        return acc_final_root(prefix_count, self.node(level, 0))

    def proof_power_of_two(self, position: int, prefix_count: int) -> AccumulatorProof:
        if prefix_count <= 0 or prefix_count & prefix_count - 1:
            raise ValueError('prefix_count must be a positive power of two')
        if not 1 <= position <= prefix_count <= self.spec.max_prefix:
            raise ValueError('position or prefix_count is invalid')
        depth = prefix_count.bit_length() - 1
        zero_based = position - 1
        siblings: list[bytes] = []
        directions: list[bool] = []
        for level in range(depth):
            node_index = zero_based >> level
            sibling_index = node_index ^ 1
            siblings.append(self.node(level, sibling_index))
            directions.append(sibling_index < node_index)
        return AccumulatorProof(prefix_count=prefix_count, position=position, path_siblings=tuple(siblings), sibling_on_left=tuple(directions), other_peaks=())

    def frontier_at(self, count: int) -> FrontierAccumulator:
        if not 0 <= count <= self.spec.max_prefix:
            raise ValueError('count is outside the dataset')
        if count == 0:
            return FrontierAccumulator()
        frontier: list[bytes | None] = [None] * count.bit_length()
        for height, start, _ in peak_layout(count):
            frontier[height] = self.node(height, start >> height)
        result = FrontierAccumulator(count=count, frontier=frontier)
        result.root()
        return result

    def iter_statements(self, start_position: int, end_position: int) -> Iterator[Any]:
        if not 1 <= start_position <= end_position <= self.spec.max_prefix:
            raise ValueError('statement range is invalid')
        for position in range(start_position, end_position + 1):
            yield self.statement(position)
