from __future__ import annotations
import hashlib
import struct
from dataclasses import dataclass
from .accumulator import DIGEST_BYTES
TAG_BLOCK_LEAF = b'MSI-E5-BLOCK-LEAF\x00'
TAG_BLOCK_NODE = b'MSI-E5-BLOCK-NODE\x00'
BLOCK_HEADER = struct.Struct('>IQQQ')

@dataclass
class CommitMetrics:
    leaf_hashes: int = 0
    internal_hashes: int = 0
    canonical_bytes: int = 0

    @property
    def hash_calls(self) -> int:
        return self.leaf_hashes + self.internal_hashes

    def as_dict(self) -> dict[str, int]:
        return {'leaf_hashes': self.leaf_hashes, 'internal_hashes': self.internal_hashes, 'hash_calls': self.hash_calls, 'canonical_bytes': self.canonical_bytes}

def canonical_block(seed: int, shard: int, epoch: int, position: int, block_size: int) -> bytes:
    if block_size < BLOCK_HEADER.size:
        raise ValueError(f'block_size must be at least {BLOCK_HEADER.size}')
    timestamp = 1700000000000 + epoch * 1000 + position
    header = BLOCK_HEADER.pack(shard, epoch, position, timestamp)
    payload_length = block_size - len(header)
    payload = hashlib.shake_256(b'MSI-E5-PAYLOAD\x00' + int(seed).to_bytes(8, 'big') + int(shard).to_bytes(4, 'big') + int(epoch).to_bytes(8, 'big') + int(position).to_bytes(8, 'big')).digest(payload_length)
    return header + payload

def block_leaf_hash(seed: int, shard: int, epoch: int, position: int, block_size: int, metrics: CommitMetrics | None=None) -> bytes:
    block = canonical_block(seed, shard, epoch, position, block_size)
    digest = hashlib.sha256(TAG_BLOCK_LEAF + len(block).to_bytes(4, 'big') + int(shard).to_bytes(4, 'big') + int(epoch).to_bytes(8, 'big') + int(position).to_bytes(8, 'big') + block).digest()
    if metrics is not None:
        metrics.leaf_hashes += 1
        metrics.canonical_bytes += len(block)
    return digest

def block_node_hash(left: bytes, right: bytes, metrics: CommitMetrics | None=None) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('Merkle children must contain 32-byte digests')
    if metrics is not None:
        metrics.internal_hashes += 1
    return hashlib.sha256(TAG_BLOCK_NODE + left + right).digest()

def next_power_of_two(value: int) -> int:
    if value <= 0:
        raise ValueError('value must be positive')
    return 1 << (value - 1).bit_length()

def commit_epoch(seed: int, shard: int, epoch: int, block_count: int, block_size: int) -> tuple[bytes, CommitMetrics]:
    if block_count <= 0:
        raise ValueError('block_count must be positive')
    metrics = CommitMetrics()
    leaves = [block_leaf_hash(seed, shard, epoch, position, block_size, metrics) for position in range(1, block_count + 1)]
    padded = next_power_of_two(block_count)
    while len(leaves) < padded:
        leaves.append(leaves[-1])
    level = leaves
    while len(level) > 1:
        level = [block_node_hash(level[index], level[index + 1], metrics) for index in range(0, len(level), 2)]
    return (level[0], metrics)
