from __future__ import annotations
import hashlib
import struct
import zlib
from .constants import AUX_REF_TAG, CANONICAL_MAGIC, CANONICAL_VERSION, CODEC_IDS, LAYOUT_IDS, PAYLOAD_REF_TAG, ROOT_STMT_TAG
from .models import CandidatePayload, Query
_CANONICAL_HEADER = struct.Struct('>4sB3xIQIQI')
CANONICAL_HEADER_BYTES = _CANONICAL_HEADER.size

def canonical_block(shard: int, epoch: int, position: int, block_bytes: int, seed: int, *, context_payload: bool=True) -> bytes:
    if block_bytes < CANONICAL_HEADER_BYTES:
        raise ValueError(f'block_bytes must be at least {CANONICAL_HEADER_BYTES}')
    payload_len = block_bytes - CANONICAL_HEADER_BYTES
    timestamp = 1700000000000000000 + epoch * 1000000 + position
    if context_payload:
        material = struct.pack('>QQIQ', seed & (1 << 64) - 1, shard, position, epoch)
    else:
        material = struct.pack('>QI', seed & (1 << 64) - 1, position)
    payload = hashlib.shake_256(b'MSI-E7-BLOCK\x00' + material).digest(payload_len)
    return _CANONICAL_HEADER.pack(CANONICAL_MAGIC, CANONICAL_VERSION, shard, epoch, position, timestamp, payload_len) + payload

def parse_canonical_block(block: bytes, q: Query, *, enforce_context: bool=True) -> bytes | None:
    if len(block) < CANONICAL_HEADER_BYTES:
        return None
    try:
        magic, version, shard, epoch, position, _timestamp, payload_len = _CANONICAL_HEADER.unpack_from(block)
    except struct.error:
        return None
    if magic != CANONICAL_MAGIC or version != CANONICAL_VERSION:
        return None
    if enforce_context and (shard, epoch, position) != q:
        return None
    if payload_len != len(block) - CANONICAL_HEADER_BYTES:
        return None
    return block

def rewrite_block_context(block: bytes, q: Query) -> bytes:
    if len(block) < CANONICAL_HEADER_BYTES:
        raise ValueError('block is too short')
    magic, version, _s, _t, _i, timestamp, payload_len = _CANONICAL_HEADER.unpack_from(block)
    return _CANONICAL_HEADER.pack(magic, version, q[0], q[1], q[2], timestamp, payload_len) + block[CANONICAL_HEADER_BYTES:]

def payload_ref(shard: int, epoch: int, layout: str, codec: str, version: int) -> bytes:
    if layout not in LAYOUT_IDS or codec not in CODEC_IDS:
        raise ValueError('unsupported layout or codec')
    return hashlib.sha256(PAYLOAD_REF_TAG + struct.pack('>IQBBH', shard, epoch, LAYOUT_IDS[layout], CODEC_IDS[codec], version)).digest()

def aux_ref(shard: int, epoch: int, mode: str, root: bytes, generation: int=1) -> bytes:
    return hashlib.sha256(AUX_REF_TAG + struct.pack('>IQI', shard, epoch, generation) + mode.encode('ascii') + b'\x00' + root).digest()

def root_statement(shard: int, epoch: int, k: int, root: bytes) -> bytes:
    return ROOT_STMT_TAG + struct.pack('>IQQ', shard, epoch, k) + root

def encode_candidate_payload(canonical: bytes, *, q: Query, layout: str, codec: str, version: int, ref: bytes) -> CandidatePayload:
    if codec == 'raw-v1':
        encoded = canonical
    elif codec == 'zlib-v1':
        encoded = zlib.compress(canonical, level=6)
    else:
        raise ValueError('unsupported codec')
    if layout == 'per_block':
        offset = 0
    elif layout == 'epoch_packed':
        offset = (q[2] - 1) * (len(canonical) + 16)
    else:
        raise ValueError('unsupported layout')
    return CandidatePayload(payload_ref=ref, layout=layout, codec=codec, version=version, position=q[2], canonical_length=len(canonical), logical_offset=offset, encoded_crc32=zlib.crc32(encoded) & 4294967295, encoded=encoded)

def decode_candidate_payload(payload: CandidatePayload, q: Query, *, enforce_context: bool=True) -> bytes | None:
    if payload.canonical_length < CANONICAL_HEADER_BYTES or payload.logical_offset < 0:
        return None
    if payload.layout == 'per_block' and payload.logical_offset != 0:
        return None
    if payload.layout == 'epoch_packed':
        expected = (q[2] - 1) * (payload.canonical_length + 16)
        if payload.logical_offset != expected:
            return None
    if zlib.crc32(payload.encoded) & 4294967295 != payload.encoded_crc32:
        return None
    try:
        if payload.codec == 'raw-v1':
            canonical = payload.encoded
        elif payload.codec == 'zlib-v1':
            canonical = zlib.decompress(payload.encoded)
        else:
            return None
    except zlib.error:
        return None
    if len(canonical) != payload.canonical_length:
        return None
    return parse_canonical_block(canonical, q, enforce_context=enforce_context)
