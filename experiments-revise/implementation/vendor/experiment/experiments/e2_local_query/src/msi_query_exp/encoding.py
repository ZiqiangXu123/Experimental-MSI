from __future__ import annotations
import hashlib
import struct
import zlib
from .constants import AUX_OBJECT_TAG, CANONICAL_HEADER_BYTES, CANONICAL_MAGIC, CANONICAL_VERSION, PAYLOAD_OBJECT_TAG, ROOT_STMT_TAG
from .crypto import OperationCounter, sha256
from .models import CandidatePayload, Query
_CANONICAL_HEADER = struct.Struct('>4sB3xIQIQI')

def canonical_block(shard: int, epoch: int, position: int, block_bytes: int, seed: int) -> bytes:
    if block_bytes < CANONICAL_HEADER_BYTES:
        raise ValueError(f'block_bytes={block_bytes} is smaller than canonical header {CANONICAL_HEADER_BYTES}')
    payload_len = block_bytes - CANONICAL_HEADER_BYTES
    timestamp = 1700000000000000000 + epoch * 1000000 + position
    seed_material = struct.pack('>QQIQ', seed & (1 << 64) - 1, shard, position, epoch)
    payload = hashlib.shake_256(b'MSI-E2-PAYLOAD-SEED\x00' + seed_material).digest(payload_len)
    header = _CANONICAL_HEADER.pack(CANONICAL_MAGIC, CANONICAL_VERSION, shard, epoch, position, timestamp, payload_len)
    return header + payload

def parse_canonical_block(block: bytes, q: Query) -> bytes | None:
    if len(block) < CANONICAL_HEADER_BYTES:
        return None
    try:
        magic, version, shard, epoch, position, _timestamp, payload_len = _CANONICAL_HEADER.unpack_from(block)
    except struct.error:
        return None
    if magic != CANONICAL_MAGIC or version != CANONICAL_VERSION:
        return None
    if (shard, epoch, position) != q:
        return None
    if payload_len != len(block) - CANONICAL_HEADER_BYTES:
        return None
    return block

def payload_ref(shard: int, epoch: int, layout: str, codec: str, version: int) -> bytes:
    body = PAYLOAD_OBJECT_TAG + struct.pack('>IQI', shard, epoch, version) + layout.encode('ascii') + b'\x00' + codec.encode('ascii')
    return hashlib.sha256(body).digest()

def aux_ref(shard: int, epoch: int, mode: str, root: bytes) -> bytes:
    return hashlib.sha256(AUX_OBJECT_TAG + struct.pack('>IQ', shard, epoch) + mode.encode('ascii') + b'\x00' + root).digest()

def root_statement(shard: int, epoch: int, k: int, root: bytes) -> bytes:
    return ROOT_STMT_TAG + struct.pack('>IQQ', shard, epoch, k) + root

def encode_candidate_payload(canonical: bytes, *, position: int, layout: str, codec: str, version: int, ref: bytes) -> CandidatePayload:
    if codec == 'raw-v1':
        encoded = canonical
    elif codec == 'zlib-v1':
        encoded = zlib.compress(canonical, level=6)
    else:
        raise ValueError(f'unsupported codec: {codec}')
    if layout == 'per_block':
        logical_offset = 0
    elif layout == 'epoch_packed':
        logical_offset = (position - 1) * (len(canonical) + 16)
    else:
        raise ValueError(f'unsupported layout: {layout}')
    return CandidatePayload(payload_ref=ref, layout=layout, codec=codec, version=version, position=position, encoded=encoded, canonical_length=len(canonical), logical_offset=logical_offset, encoded_crc32=zlib.crc32(encoded) & 4294967295)

def check_payload_envelope(payload: CandidatePayload, q: Query) -> bool:
    if payload.position != q[2]:
        return False
    if payload.canonical_length < CANONICAL_HEADER_BYTES:
        return False
    if payload.logical_offset < 0:
        return False
    if payload.layout == 'per_block' and payload.logical_offset != 0:
        return False
    if payload.layout == 'epoch_packed':
        expected_offset = (q[2] - 1) * (payload.canonical_length + 16)
        if payload.logical_offset != expected_offset:
            return False
    if zlib.crc32(payload.encoded) & 4294967295 != payload.encoded_crc32:
        return False
    return True

def decode_candidate_payload(payload: CandidatePayload, q: Query, counter: OperationCounter | None=None) -> bytes | None:
    try:
        if payload.codec == 'raw-v1':
            canonical = payload.encoded
        elif payload.codec == 'zlib-v1':
            canonical = zlib.decompress(payload.encoded)
            if counter is not None:
                counter.record_allocation(canonical)
        else:
            return None
    except zlib.error:
        return None
    if len(canonical) != payload.canonical_length:
        return None
    return parse_canonical_block(canonical, q)
