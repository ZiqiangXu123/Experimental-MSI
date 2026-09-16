from __future__ import annotations
import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Iterable
from .constants import ANCHOR_RECORD_BYTES, AUX_VALUE_HEADER_BYTES, CANONICAL_BLOCK_HEADER_BYTES, CODEC_IDS, DIGEST_BYTES, LAYOUT_IDS, MODE_IDS, MSI_ENTRY_BYTES, PAYLOAD_VALUE_HEADER_BYTES, ROOT_INDEX_RECORD_BYTES
LEAF_TAG = b'MSI-LEAF-v1\x00'
INTERNAL_TAG = b'MSI-NODE-v1\x00'

def u64_context(*values: int) -> bytes:
    out = bytearray()
    for value in values:
        out.extend(struct.pack('>Q', int(value)))
    return bytes(out)

def digest(domain: bytes, *parts: bytes) -> bytes:
    h = hashlib.sha256()
    h.update(domain)
    for part in parts:
        h.update(part)
    return h.digest()

def deterministic_bytes(seed: int, shard: int, epoch: int, position: int, length: int) -> bytes:
    if length < 0:
        raise ValueError('length must be non-negative')
    shake = hashlib.shake_256()
    shake.update(b'MSI-E1-PAYLOAD-v1\x00')
    shake.update(struct.pack('>QQQQ', seed & (1 << 64) - 1, shard, epoch, position))
    return shake.digest(length)

def canonical_block(*, shard: int, epoch: int, position: int, block_bytes: int, seed: int) -> bytes:
    if block_bytes < CANONICAL_BLOCK_HEADER_BYTES:
        raise ValueError(f'block_bytes={block_bytes} is smaller than canonical header ({CANONICAL_BLOCK_HEADER_BYTES})')
    payload_len = block_bytes - CANONICAL_BLOCK_HEADER_BYTES
    timestamp = 1700000000000 + epoch * 1000000 + position
    header = struct.pack('>4sHIQIQI', b'BLK1', 1, shard, epoch, position, timestamp, payload_len)
    body = deterministic_bytes(seed, shard, epoch, position, payload_len)
    result = header + body
    if len(result) != block_bytes:
        raise AssertionError('canonical block encoder produced an unexpected length')
    return result

def leaf_hash(shard: int, epoch: int, position: int, block: bytes) -> bytes:
    return digest(LEAF_TAG, struct.pack('>I', shard), struct.pack('>Q', epoch), struct.pack('>I', position), struct.pack('>I', len(block)), block)

def internal_hash(left: bytes, right: bytes) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('internal hash inputs must be digest-sized')
    return digest(INTERNAL_TAG, left, right)

@dataclass(frozen=True)
class MerkleMaterial:
    root: bytes
    padded_leaves: tuple[bytes, ...]
    non_root_nodes: tuple[bytes, ...]
    n_prime: int

def build_merkle(leaves: list[bytes]) -> MerkleMaterial:
    if not leaves:
        raise ValueError('empty epochs are excluded')
    for leaf in leaves:
        if len(leaf) != DIGEST_BYTES:
            raise ValueError('all leaves must be digest-sized')
    n = len(leaves)
    n_prime = 1 << (n - 1).bit_length()
    padded = list(leaves)
    padded.extend([leaves[-1]] * (n_prime - n))
    if n_prime == 1:
        return MerkleMaterial(root=padded[0], padded_leaves=tuple(padded), non_root_nodes=tuple(), n_prime=n_prime)
    non_root: list[bytes] = list(padded)
    level = padded
    while len(level) > 1:
        parents = [internal_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        if len(parents) > 1:
            non_root.extend(parents)
        level = parents
    expected = 2 * n_prime - 2
    if len(non_root) != expected:
        raise AssertionError(f'non-root node count {len(non_root)} != {expected}')
    return MerkleMaterial(root=level[0], padded_leaves=tuple(padded), non_root_nodes=tuple(non_root), n_prime=n_prime)

def synthetic_root(seed: int, shard: int, epoch: int, n: int, block_bytes: int) -> bytes:
    return digest(b'MSI-E1-SYNTHETIC-ROOT-v1\x00', struct.pack('>QQQQQ', seed, shard, epoch, n, block_bytes))

def object_ref(kind: str, shard: int, epoch: int, variant: str) -> bytes:
    return digest(b'MSI-E1-OBJECT-REF-v1\x00', kind.encode('ascii'), variant.encode('ascii'), struct.pack('>IQ', shard, epoch))

def encode_msi_entry(*, epoch_position: int, n: int, root: bytes, payload_ref: bytes, aux_ref: bytes, layout: str, mode: str, codec: str, version: int=1) -> bytes:
    if len(root) != DIGEST_BYTES or len(payload_ref) != DIGEST_BYTES or len(aux_ref) != DIGEST_BYTES:
        raise ValueError('root and references must be 32 bytes')
    body = struct.pack('>QI32s32s32sBBBHB', epoch_position, n, root, payload_ref, aux_ref, LAYOUT_IDS[layout], MODE_IDS[mode], CODEC_IDS[codec], version, 0)
    prefix = struct.pack('>4sI', b'MSI1', len(body) + 4)
    crc = struct.pack('>I', zlib.crc32(prefix + body) & 4294967295)
    encoded = prefix + body + crc
    if len(encoded) != MSI_ENTRY_BYTES:
        raise AssertionError(f'MSI entry is {len(encoded)} bytes, expected {MSI_ENTRY_BYTES}')
    return encoded

def encode_root_index_record(epoch_position: int, root: bytes) -> bytes:
    body = struct.pack('>4sQ32s', b'ROT1', epoch_position, root)
    crc = struct.pack('>I', zlib.crc32(body) & 4294967295)
    encoded = body + crc
    if len(encoded) != ROOT_INDEX_RECORD_BYTES:
        raise AssertionError('root-index record length mismatch')
    return encoded

def encode_anchor_record(shard: int, checkpoint_k: int, root: bytes, seed: int) -> bytes:
    key_id = digest(b'MSI-E1-KEY-ID\x00', struct.pack('>IQ', shard, seed))[:16]
    sig_left = digest(b'MSI-E1-SIG-L\x00', root, struct.pack('>IQ', shard, checkpoint_k))
    sig_right = digest(b'MSI-E1-SIG-R\x00', root, struct.pack('>IQ', shard, checkpoint_k))
    signature = sig_left + sig_right
    body = struct.pack('>4sBIQ32s16s64s', b'ANC1', 1, shard, checkpoint_k, root, key_id, signature)
    crc = struct.pack('>I', zlib.crc32(body) & 4294967295)
    encoded = body + crc
    if len(encoded) != ANCHOR_RECORD_BYTES:
        raise AssertionError('anchor record length mismatch')
    return encoded

def encode_aux_value(mode: str, n: int, n_prime: int, hashes: Iterable[bytes]) -> bytes:
    hash_list = list(hashes)
    for item in hash_list:
        if len(item) != DIGEST_BYTES:
            raise ValueError('auxiliary hashes must be digest-sized')
    header = struct.pack('>4sBBHIIII', b'AUX1', MODE_IDS[mode], 1, DIGEST_BYTES, n, n_prime, len(hash_list), 0)
    if len(header) != AUX_VALUE_HEADER_BYTES:
        raise AssertionError('auxiliary header length mismatch')
    return header + b''.join(hash_list)

def _apply_codec(data: bytes, codec: str) -> bytes:
    if codec == 'raw-v1':
        return data
    if codec == 'zlib-v1':
        return zlib.compress(data, level=6)
    raise KeyError(codec)

def encode_payload_values(*, blocks: list[bytes], layout: str, codec: str) -> list[bytes]:
    if layout == 'per_block':
        result = []
        for block in blocks:
            encoded = _apply_codec(block, codec)
            header = struct.pack('>4sBBBBQI', b'PAY1', LAYOUT_IDS[layout], CODEC_IDS[codec], 1, 0, len(block), len(encoded))
            if len(header) != PAYLOAD_VALUE_HEADER_BYTES:
                raise AssertionError('payload header length mismatch')
            result.append(header + encoded)
        return result
    if layout == 'epoch_packed':
        offsets = [0]
        concat = bytearray()
        for block in blocks:
            concat.extend(block)
            offsets.append(len(concat))
        table = struct.pack('>I', len(blocks)) + b''.join((struct.pack('>Q', x) for x in offsets))
        canonical_epoch = table + bytes(concat)
        encoded = _apply_codec(canonical_epoch, codec)
        header = struct.pack('>4sBBBBQI', b'PAY1', LAYOUT_IDS[layout], CODEC_IDS[codec], 1, 0, len(canonical_epoch), len(encoded))
        if len(header) != PAYLOAD_VALUE_HEADER_BYTES:
            raise AssertionError('payload header length mismatch')
        return [header + encoded]
    raise KeyError(layout)
