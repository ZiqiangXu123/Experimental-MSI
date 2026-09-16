from __future__ import annotations
import hashlib
import json
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence
from .util import atomic_write_bytes, atomic_write_json, read_json, sha256_file
LEAF_TAG = b'\x00MSI-E6-LEAF\x00'
INTERNAL_TAG = b'\x01MSI-E6-INTERNAL\x00'
BLOCK_MAGIC = b'E6B1'
BLOCK_HEADER = struct.Struct('>4sIQIQI')
SUPPORT_HEADER = struct.Struct('>8sB3xIIH2x32sI4x')
FULL_MAGIC = b'E6FULL1\x00'
LEAF_MAGIC = b'E6LEAF1\x00'
PAYLOAD_INDEX_HEADER = struct.Struct('>8sB3xIII')
PAYLOAD_INDEX_ENTRY = struct.Struct('>QI')
PAYLOAD_INDEX_MAGIC = b'E6PIDX1\x00'

class FormatError(ValueError):
    pass

@dataclass
class HashCounter:
    leaf: int = 0
    internal: int = 0

    @property
    def total(self) -> int:
        return self.leaf + self.internal

    def as_dict(self) -> dict[str, int]:
        return {'leaf': self.leaf, 'internal': self.internal, 'total': self.total}

@dataclass
class IOStats:
    bytes_read: int = 0
    bytes_written: int = 0

    def read(self, value: int) -> None:
        self.bytes_read += int(value)

    def write(self, value: int) -> None:
        self.bytes_written += int(value)

    def as_dict(self) -> dict[str, int]:
        return {'bytes_read': self.bytes_read, 'bytes_written': self.bytes_written}

def next_power_of_two(n: int) -> int:
    if n < 1:
        raise ValueError('epoch length must be positive')
    return 1 << (n - 1).bit_length()

def merkle_depth(n_prime: int) -> int:
    if n_prime < 1 or n_prime & n_prime - 1:
        raise ValueError('padded length must be a positive power of two')
    return n_prime.bit_length() - 1

def _u64(value: int) -> bytes:
    return int(value).to_bytes(8, 'big', signed=False)

def canonical_block(*, shard: int, epoch: int, position: int, block_bytes: int, seed: int) -> bytes:
    if block_bytes < BLOCK_HEADER.size:
        raise ValueError(f'block_bytes must be >= {BLOCK_HEADER.size}')
    payload_length = block_bytes - BLOCK_HEADER.size
    timestamp = 1700000000000000 + epoch * 1000000 + position
    header = BLOCK_HEADER.pack(BLOCK_MAGIC, int(shard), int(epoch), int(position), int(timestamp), int(payload_length))
    shake = hashlib.shake_256()
    shake.update(b'MSI-E6-BLOCK\x00')
    shake.update(_u64(seed))
    shake.update(_u64(shard))
    shake.update(_u64(epoch))
    shake.update(_u64(position))
    return header + shake.digest(payload_length)

def validate_canonical_block(block: bytes, *, shard: int, epoch: int, position: int, block_bytes: int) -> None:
    if len(block) != block_bytes:
        raise FormatError('block length mismatch')
    try:
        magic, got_shard, got_epoch, got_position, _timestamp, payload_length = BLOCK_HEADER.unpack_from(block)
    except struct.error as exc:
        raise FormatError('truncated block header') from exc
    if magic != BLOCK_MAGIC:
        raise FormatError('invalid block magic')
    if (got_shard, got_epoch, got_position) != (shard, epoch, position):
        raise FormatError('block context mismatch')
    if payload_length != block_bytes - BLOCK_HEADER.size:
        raise FormatError('block payload length mismatch')

def leaf_hash(shard: int, epoch: int, position: int, block: bytes, counter: HashCounter | None=None) -> bytes:
    if counter is not None:
        counter.leaf += 1
    return hashlib.sha256(LEAF_TAG + _u64(shard) + _u64(epoch) + _u64(position) + len(block).to_bytes(8, 'big') + block).digest()

def internal_hash(left: bytes, right: bytes, counter: HashCounter | None=None) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise ValueError('Merkle children must be 32-byte digests')
    if counter is not None:
        counter.internal += 1
    return hashlib.sha256(INTERNAL_TAG + left + right).digest()

def pad_leaves(leaves: Sequence[bytes], n_prime: int | None=None) -> list[bytes]:
    if not leaves:
        raise ValueError('empty epochs are excluded')
    target = next_power_of_two(len(leaves)) if n_prime is None else int(n_prime)
    if target < len(leaves) or target & target - 1:
        raise ValueError('invalid padded length')
    result = list(leaves)
    if any((len(value) != 32 for value in result)):
        raise ValueError('leaf digest length mismatch')
    result.extend([result[-1]] * (target - len(result)))
    return result

def build_layers(leaves: Sequence[bytes], counter: HashCounter | None=None) -> list[list[bytes]]:
    if not leaves or len(leaves) & len(leaves) - 1:
        raise ValueError('leaf layer must have power-of-two length')
    layers = [list(leaves)]
    while len(layers[-1]) > 1:
        current = layers[-1]
        layers.append([internal_hash(current[i], current[i + 1], counter) for i in range(0, len(current), 2)])
    return layers

def build_root(leaves: Sequence[bytes], counter: HashCounter | None=None) -> bytes:
    return build_layers(leaves, counter)[-1][0]

def path_from_layers(layers: Sequence[Sequence[bytes]], position: int) -> list[tuple[bytes, int]]:
    if not layers:
        raise ValueError('missing layers')
    if position < 1 or position > len(layers[0]):
        raise ValueError('position out of range')
    p = position - 1
    output: list[tuple[bytes, int]] = []
    for layer in layers[:-1]:
        sibling = p + 1 if p % 2 == 0 else p - 1
        direction = 0 if p % 2 == 0 else 1
        output.append((bytes(layer[sibling]), direction))
        p //= 2
    return output

def rebuild_path_from_leaf_vector(leaves: Sequence[bytes], position: int, counter: HashCounter | None=None) -> list[tuple[bytes, int]]:
    if not leaves or len(leaves) & len(leaves) - 1:
        raise ValueError('leaf vector must have power-of-two length')
    if position < 1 or position > len(leaves):
        raise ValueError('position out of range')
    working = list(leaves)
    p = position - 1
    output: list[tuple[bytes, int]] = []
    while len(working) > 1:
        sibling = p + 1 if p % 2 == 0 else p - 1
        direction = 0 if p % 2 == 0 else 1
        output.append((working[sibling], direction))
        working = [internal_hash(working[i], working[i + 1], counter) for i in range(0, len(working), 2)]
        p //= 2
    return output

def verify_path(*, shard: int, epoch: int, position: int, block: bytes, path: Sequence[tuple[bytes, int]], expected_root: bytes, counter: HashCounter | None=None) -> bool:
    value = leaf_hash(shard, epoch, position, block, counter)
    for sibling, direction in path:
        if direction == 0:
            value = internal_hash(value, sibling, counter)
        elif direction == 1:
            value = internal_hash(sibling, value, counter)
        else:
            return False
    return value == expected_root

def serialize_full_support(*, n: int, layers: Sequence[Sequence[bytes]]) -> bytes:
    n_prime = len(layers[0])
    body = b'' if n_prime == 1 else b''.join((node for layer in layers[:-1] for node in layer))
    expected_hashes = 2 * n_prime - 2
    if len(body) != expected_hashes * 32:
        raise AssertionError('full support formula mismatch')
    header = SUPPORT_HEADER.pack(FULL_MAGIC, 1, int(n), int(n_prime), merkle_depth(n_prime), bytes(layers[-1][0]), zlib.crc32(body) & 4294967295)
    return header + body

def serialize_leaf_support(*, n: int, leaves: Sequence[bytes], root: bytes) -> bytes:
    body = b''.join(leaves)
    header = SUPPORT_HEADER.pack(LEAF_MAGIC, 1, int(n), len(leaves), merkle_depth(len(leaves)), bytes(root), zlib.crc32(body) & 4294967295)
    return header + body

@dataclass(frozen=True)
class SupportState:
    mode: str
    n: int
    n_prime: int
    depth: int
    root: bytes
    leaves: tuple[bytes, ...]
    layers: tuple[tuple[bytes, ...], ...]
    serialized_bytes: int

def parse_support_bytes(data: bytes, *, expected_mode: str | None=None, counter: HashCounter | None=None, verify: bool=True) -> SupportState:
    if len(data) < SUPPORT_HEADER.size:
        raise FormatError('truncated support header')
    magic, version, n, n_prime, depth, root, body_crc = SUPPORT_HEADER.unpack_from(data)
    if version != 1 or n < 1 or n_prime != next_power_of_two(n) or (depth != merkle_depth(n_prime)):
        raise FormatError('invalid support metadata')
    body = data[SUPPORT_HEADER.size:]
    if zlib.crc32(body) & 4294967295 != body_crc:
        raise FormatError('support CRC mismatch')
    if magic == FULL_MAGIC:
        mode = 'full'
        expected_hashes = 2 * n_prime - 2
    elif magic == LEAF_MAGIC:
        mode = 'leaf'
        expected_hashes = n_prime
    else:
        raise FormatError('unknown support magic')
    if expected_mode is not None and mode != expected_mode:
        raise FormatError('support mode mismatch')
    if len(body) != expected_hashes * 32:
        raise FormatError('support body length mismatch')
    hashes = [body[i:i + 32] for i in range(0, len(body), 32)]
    if mode == 'leaf':
        leaves = hashes
        layers = build_layers(leaves, counter) if verify else [leaves]
        if verify and layers[-1][0] != root:
            raise FormatError('leaf support root mismatch')
    elif n_prime == 1:
        leaves = []
        layers = []
    else:
        stored: list[list[bytes]] = []
        offset = 0
        size = n_prime
        while size >= 2:
            stored.append(hashes[offset:offset + size])
            offset += size
            size //= 2
        if offset != len(hashes):
            raise FormatError('full layer partition mismatch')
        leaves = stored[0]
        if verify:
            for level in range(len(stored) - 1):
                expected = [internal_hash(stored[level][i], stored[level][i + 1], counter) for i in range(0, len(stored[level]), 2)]
                if expected != stored[level + 1]:
                    raise FormatError('full support internal layer mismatch')
            computed_root = internal_hash(stored[-1][0], stored[-1][1], counter)
            if computed_root != root:
                raise FormatError('full support root mismatch')
            layers = stored + [[computed_root]]
        else:
            layers = stored
    return SupportState(mode=mode, n=n, n_prime=n_prime, depth=depth, root=root, leaves=tuple(leaves), layers=tuple((tuple(layer) for layer in layers)), serialized_bytes=len(data))

def read_support(path: str | Path, *, expected_mode: str | None=None, counter: HashCounter | None=None, io_stats: IOStats | None=None, verify: bool=True) -> SupportState:
    data = Path(path).read_bytes()
    if io_stats is not None:
        io_stats.read(len(data))
    return parse_support_bytes(data, expected_mode=expected_mode, counter=counter, verify=verify)

def write_support(path: str | Path, data: bytes, io_stats: IOStats | None=None) -> None:
    atomic_write_bytes(path, data)
    if io_stats is not None:
        io_stats.write(len(data))

def _codec_encode(block: bytes, codec: str) -> bytes:
    if codec == 'raw-v1':
        return block
    if codec == 'zlib-v1':
        return zlib.compress(block, level=6)
    raise ValueError(f'unsupported codec: {codec}')

def _codec_decode(data: bytes, codec: str) -> bytes:
    if codec == 'raw-v1':
        return data
    if codec == 'zlib-v1':
        try:
            return zlib.decompress(data)
        except zlib.error as exc:
            raise FormatError('compressed block failed to decode') from exc
    raise FormatError(f'unsupported codec: {codec}')

def build_payload_store(root: str | Path, *, shard: int, epoch: int, n: int, block_bytes: int, layout: str, codec: str, seed: int) -> dict[str, object]:
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    encoded_total = 0
    logical_total = 0
    if layout == 'epoch_packed':
        data_path = target / 'payload.dat'
        offsets: list[tuple[int, int]] = []
        offset = 0
        with data_path.open('wb') as handle:
            for position in range(1, n + 1):
                block = canonical_block(shard=shard, epoch=epoch, position=position, block_bytes=block_bytes, seed=seed)
                encoded = _codec_encode(block, codec)
                handle.write(encoded)
                offsets.append((offset, len(encoded)))
                offset += len(encoded)
                encoded_total += len(encoded)
                logical_total += len(block)
            handle.flush()
            os.fsync(handle.fileno())
        header = PAYLOAD_INDEX_HEADER.pack(PAYLOAD_INDEX_MAGIC, 1, n, block_bytes, 0 if codec == 'raw-v1' else 1)
        body = b''.join((PAYLOAD_INDEX_ENTRY.pack(off, length) for off, length in offsets))
        atomic_write_bytes(target / 'payload.idx', header + body)
    elif layout == 'per_block':
        blocks = target / 'blocks'
        blocks.mkdir(parents=True, exist_ok=True)
        for position in range(1, n + 1):
            block = canonical_block(shard=shard, epoch=epoch, position=position, block_bytes=block_bytes, seed=seed)
            encoded = _codec_encode(block, codec)
            atomic_write_bytes(blocks / f'{position:08d}.bin', encoded)
            encoded_total += len(encoded)
            logical_total += len(block)
    else:
        raise ValueError(f'unsupported layout: {layout}')
    manifest = {'schema_version': 1, 'shard': shard, 'epoch': epoch, 'n': n, 'n_prime': next_power_of_two(n), 'block_bytes': block_bytes, 'layout': layout, 'codec': codec, 'seed': seed, 'encoded_bytes': encoded_total, 'logical_bytes': logical_total}
    atomic_write_json(target / 'manifest.json', manifest)
    return manifest

class PayloadStore:

    def __init__(self, root: str | Path, io_stats: IOStats | None=None):
        self.root = Path(root)
        self.manifest = read_json(self.root / 'manifest.json')
        self.io_stats = io_stats
        self.layout = str(self.manifest['layout'])
        self.codec = str(self.manifest['codec'])
        self.n = int(self.manifest['n'])
        self.block_bytes = int(self.manifest['block_bytes'])
        self.shard = int(self.manifest['shard'])
        self.epoch = int(self.manifest['epoch'])
        self._index: list[tuple[int, int]] | None = None
        if self.layout == 'epoch_packed':
            raw = (self.root / 'payload.idx').read_bytes()
            if self.io_stats is not None:
                self.io_stats.read(len(raw))
            if len(raw) < PAYLOAD_INDEX_HEADER.size:
                raise FormatError('truncated payload index')
            magic, version, n, block_bytes, codec_id = PAYLOAD_INDEX_HEADER.unpack_from(raw)
            expected_codec_id = 0 if self.codec == 'raw-v1' else 1
            if (magic, version, n, block_bytes, codec_id) != (PAYLOAD_INDEX_MAGIC, 1, self.n, self.block_bytes, expected_codec_id):
                raise FormatError('payload index metadata mismatch')
            body = raw[PAYLOAD_INDEX_HEADER.size:]
            if len(body) != self.n * PAYLOAD_INDEX_ENTRY.size:
                raise FormatError('payload index length mismatch')
            self._index = [PAYLOAD_INDEX_ENTRY.unpack_from(body, i * PAYLOAD_INDEX_ENTRY.size) for i in range(self.n)]

    def read_block(self, position: int) -> bytes:
        if position < 1 or position > self.n:
            raise IndexError(position)
        if self.layout == 'epoch_packed':
            assert self._index is not None
            offset, length = self._index[position - 1]
            with (self.root / 'payload.dat').open('rb') as handle:
                handle.seek(offset)
                encoded = handle.read(length)
            if len(encoded) != length:
                raise FormatError('truncated packed payload')
        else:
            encoded = (self.root / 'blocks' / f'{position:08d}.bin').read_bytes()
        if self.io_stats is not None:
            self.io_stats.read(len(encoded))
        block = _codec_decode(encoded, self.codec)
        validate_canonical_block(block, shard=self.shard, epoch=self.epoch, position=position, block_bytes=self.block_bytes)
        return block

    def iter_blocks(self) -> Iterator[tuple[int, bytes]]:
        for position in range(1, self.n + 1):
            yield (position, self.read_block(position))

def leaves_from_payload(payload_root: str | Path, counter: HashCounter | None=None, io_stats: IOStats | None=None) -> tuple[list[bytes], dict[str, object]]:
    store = PayloadStore(payload_root, io_stats=io_stats)
    leaves = [leaf_hash(store.shard, store.epoch, position, block, counter) for position, block in store.iter_blocks()]
    return (pad_leaves(leaves), dict(store.manifest))

def payload_fingerprint(root: str | Path) -> str:
    target = Path(root)
    digest = hashlib.sha256()
    for path in sorted((p for p in target.rglob('*') if p.is_file())):
        relative = path.relative_to(target).as_posix().encode()
        digest.update(len(relative).to_bytes(4, 'big'))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()

def support_size_formula(mode: str, n: int) -> int:
    n_prime = next_power_of_two(n)
    if mode == 'full':
        return SUPPORT_HEADER.size + (2 * n_prime - 2) * 32
    if mode == 'leaf':
        return SUPPORT_HEADER.size + n_prime * 32
    if mode == 'ext':
        return 0
    raise ValueError(mode)
