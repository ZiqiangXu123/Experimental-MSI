from __future__ import annotations
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from .constants import ANCHOR_IDS, ANCHOR_MAGIC, ANCHOR_NAMES, CODEC_IDS, CODEC_NAMES, DIGEST_BYTES, ED25519_PUBLIC_BYTES, ED25519_SIGNATURE_BYTES, FIELD_ANCHOR, FIELD_META, FIELD_NAMES, FIELD_PAYLOAD, FIELD_QUERY, FIELD_WITNESS, FRAME_MAGIC, FRAME_VERSION, LAYOUT_IDS, LAYOUT_NAMES, MAX_CANONICAL_BYTES, MAX_FIELD_COUNT, MAX_FRAME_BYTES, MAX_TLV_BYTES, MAX_WITNESS_ITEMS, MODE_IDS, MODE_NAMES, PAYLOAD_MAGIC, REQUIRED_FIELDS, WITNESS_LEAF_VECTOR, WITNESS_MAGIC, WITNESS_PATH
from .models import AggregateCertificate, CandidatePayload, DirectCertificate, Meta, ParsedFrame, Response
from .util import is_power_of_two, next_power_of_two
Guard = Callable[[str], None]
_FRAME_HEADER = struct.Struct('>4sBBHI')
_FIELD_HEADER = struct.Struct('>BBI')
_QUERY = struct.Struct('>IQI')
_META = struct.Struct('>32sIQ32s32sBBBBH')
_PAYLOAD_HEADER = struct.Struct('>4sBBBBHIIQII')
_WITNESS_HEADER = struct.Struct('>4sBBBBII32s')
_ANCHOR_HEADER = struct.Struct('>4sBBH')
_DIRECT_PREFIX = struct.Struct('>32sH')
_AGG_PREFIX = struct.Struct('>32sIQ32s64sQH')
_PATH_ITEM = struct.Struct('>32sB')

class ProtocolError(ValueError):

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

def _mark(mark: Guard | None, guard: str) -> None:
    if mark is not None:
        mark(guard)

def encode_query(q: tuple[int, int, int]) -> bytes:
    return _QUERY.pack(q[0], q[1], q[2])

def decode_query(data: bytes, mark: Guard | None=None) -> tuple[int, int, int]:
    _mark(mark, 'query.decode')
    if len(data) != _QUERY.size:
        raise ProtocolError('query_length')
    return _QUERY.unpack(data)

def encode_meta(root: bytes, meta: Meta) -> bytes:
    if len(root) != DIGEST_BYTES:
        raise ValueError('root must be 32 bytes')
    return _META.pack(root, meta.n, meta.k, meta.payload_ref, meta.aux_ref, LAYOUT_IDS[meta.layout], MODE_IDS[meta.mode], CODEC_IDS[meta.codec], 0, meta.version)

def decode_meta(data: bytes, mark: Guard | None=None) -> tuple[bytes, Meta]:
    _mark(mark, 'meta.decode')
    if len(data) != _META.size:
        raise ProtocolError('meta_length')
    root, n, k, payload_ref, aux_ref, layout_id, mode_id, codec_id, reserved, version = _META.unpack(data)
    if reserved != 0:
        raise ProtocolError('meta_reserved')
    if layout_id not in LAYOUT_NAMES or mode_id not in MODE_NAMES or codec_id not in CODEC_NAMES:
        raise ProtocolError('meta_enum')
    return (root, Meta(n=n, k=k, payload_ref=payload_ref, aux_ref=aux_ref, layout=LAYOUT_NAMES[layout_id], mode=MODE_NAMES[mode_id], codec=CODEC_NAMES[codec_id], version=version))

def encode_payload(payload: CandidatePayload) -> bytes:
    encoded = payload.encoded
    return _PAYLOAD_HEADER.pack(PAYLOAD_MAGIC, FRAME_VERSION, LAYOUT_IDS[payload.layout], CODEC_IDS[payload.codec], 0, payload.version, payload.position, payload.canonical_length, payload.logical_offset, len(encoded), payload.encoded_crc32) + payload.payload_ref + encoded

def decode_payload(data: bytes, mark: Guard | None=None) -> CandidatePayload:
    _mark(mark, 'payload.decode')
    minimum = _PAYLOAD_HEADER.size + DIGEST_BYTES
    if len(data) < minimum:
        raise ProtocolError('payload_truncated')
    magic, schema, layout_id, codec_id, reserved, version, position, canonical_length, logical_offset, encoded_length, encoded_crc32 = _PAYLOAD_HEADER.unpack_from(data)
    if magic != PAYLOAD_MAGIC or schema != FRAME_VERSION or reserved != 0:
        raise ProtocolError('payload_header')
    if layout_id not in LAYOUT_NAMES or codec_id not in CODEC_NAMES:
        raise ProtocolError('payload_enum')
    if canonical_length > MAX_CANONICAL_BYTES or encoded_length > MAX_TLV_BYTES:
        raise ProtocolError('payload_too_large')
    expected = minimum + encoded_length
    if len(data) != expected:
        raise ProtocolError('payload_length')
    payload_ref = data[_PAYLOAD_HEADER.size:minimum]
    encoded = data[minimum:]
    return CandidatePayload(payload_ref=payload_ref, layout=LAYOUT_NAMES[layout_id], codec=CODEC_NAMES[codec_id], version=version, position=position, canonical_length=canonical_length, logical_offset=logical_offset, encoded_crc32=encoded_crc32, encoded=encoded)

def encode_witness(mode: str, aux_ref: bytes, witness: tuple[Any, ...], n: int) -> bytes:
    n_prime = next_power_of_two(n)
    if mode == 'leaf':
        kind = WITNESS_LEAF_VECTOR
        body = b''.join(witness)
        count = len(witness)
    else:
        kind = WITNESS_PATH
        body = b''.join((_PATH_ITEM.pack(digest, direction) for digest, direction in witness))
        count = len(witness)
    return _WITNESS_HEADER.pack(WITNESS_MAGIC, FRAME_VERSION, kind, MODE_IDS[mode], 0, n_prime, count, aux_ref) + body

def decode_witness(data: bytes, mark: Guard | None=None) -> tuple[str, bytes, tuple[Any, ...], int]:
    _mark(mark, 'witness.decode')
    if len(data) < _WITNESS_HEADER.size:
        raise ProtocolError('witness_truncated')
    magic, schema, kind, mode_id, reserved, n_prime, count, aux_ref = _WITNESS_HEADER.unpack_from(data)
    if magic != WITNESS_MAGIC or schema != FRAME_VERSION or reserved != 0:
        raise ProtocolError('witness_header')
    if mode_id not in MODE_NAMES or kind not in (WITNESS_PATH, WITNESS_LEAF_VECTOR):
        raise ProtocolError('witness_enum')
    if count > MAX_WITNESS_ITEMS or not is_power_of_two(n_prime):
        raise ProtocolError('witness_shape')
    mode = MODE_NAMES[mode_id]
    if kind == WITNESS_PATH:
        expected = _WITNESS_HEADER.size + count * _PATH_ITEM.size
        if len(data) != expected:
            raise ProtocolError('witness_path_length')
        values: list[tuple[bytes, int]] = []
        offset = _WITNESS_HEADER.size
        for _ in range(count):
            digest, direction = _PATH_ITEM.unpack_from(data, offset)
            values.append((digest, direction))
            offset += _PATH_ITEM.size
        witness: tuple[Any, ...] = tuple(values)
    else:
        expected = _WITNESS_HEADER.size + count * DIGEST_BYTES
        if len(data) != expected:
            raise ProtocolError('witness_vector_length')
        witness = tuple((data[offset:offset + DIGEST_BYTES] for offset in range(_WITNESS_HEADER.size, expected, DIGEST_BYTES)))
    return (mode, aux_ref, witness, n_prime)

def encode_anchor(certificate: DirectCertificate | AggregateCertificate) -> bytes:
    if isinstance(certificate, DirectCertificate):
        return _ANCHOR_HEADER.pack(ANCHOR_MAGIC, FRAME_VERSION, ANCHOR_IDS['direct'], 0) + _DIRECT_PREFIX.pack(certificate.public_key, len(certificate.statement)) + certificate.statement + certificate.signature
    body = _ANCHOR_HEADER.pack(ANCHOR_MAGIC, FRAME_VERSION, ANCHOR_IDS['aggregate'], 0) + _AGG_PREFIX.pack(certificate.public_key, certificate.shard, certificate.k_star, certificate.accumulator_root, certificate.checkpoint_signature, certificate.statement_position, len(certificate.statement)) + certificate.statement + struct.pack('>H', len(certificate.inclusion_path)) + b''.join((_PATH_ITEM.pack(digest, direction) for digest, direction in certificate.inclusion_path))
    return body

def decode_anchor(data: bytes, mark: Guard | None=None) -> DirectCertificate | AggregateCertificate:
    _mark(mark, 'anchor.decode')
    if len(data) < _ANCHOR_HEADER.size:
        raise ProtocolError('anchor_truncated')
    magic, schema, anchor_id, reserved = _ANCHOR_HEADER.unpack_from(data)
    if magic != ANCHOR_MAGIC or schema != FRAME_VERSION or reserved != 0:
        raise ProtocolError('anchor_header')
    if anchor_id not in ANCHOR_NAMES:
        raise ProtocolError('anchor_type')
    offset = _ANCHOR_HEADER.size
    if ANCHOR_NAMES[anchor_id] == 'direct':
        minimum = offset + _DIRECT_PREFIX.size + ED25519_SIGNATURE_BYTES
        if len(data) < minimum:
            raise ProtocolError('direct_anchor_truncated')
        public_key, statement_length = _DIRECT_PREFIX.unpack_from(data, offset)
        offset += _DIRECT_PREFIX.size
        expected = offset + statement_length + ED25519_SIGNATURE_BYTES
        if statement_length > MAX_TLV_BYTES or len(data) != expected:
            raise ProtocolError('direct_anchor_length')
        statement = data[offset:offset + statement_length]
        signature = data[offset + statement_length:]
        return DirectCertificate(public_key=public_key, statement=statement, signature=signature)
    minimum = offset + _AGG_PREFIX.size + 2
    if len(data) < minimum:
        raise ProtocolError('aggregate_anchor_truncated')
    public_key, shard, k_star, acc_root, signature, statement_position, statement_length = _AGG_PREFIX.unpack_from(data, offset)
    offset += _AGG_PREFIX.size
    if statement_length > MAX_TLV_BYTES or len(data) < offset + statement_length + 2:
        raise ProtocolError('aggregate_statement_length')
    statement = data[offset:offset + statement_length]
    offset += statement_length
    proof_count = struct.unpack_from('>H', data, offset)[0]
    offset += 2
    expected = offset + proof_count * _PATH_ITEM.size
    if proof_count > 64 or len(data) != expected:
        raise ProtocolError('aggregate_proof_length')
    path: list[tuple[bytes, int]] = []
    for _ in range(proof_count):
        digest, direction = _PATH_ITEM.unpack_from(data, offset)
        path.append((digest, direction))
        offset += _PATH_ITEM.size
    return AggregateCertificate(public_key=public_key, shard=shard, k_star=k_star, accumulator_root=acc_root, checkpoint_signature=signature, statement=statement, statement_position=statement_position, inclusion_path=tuple(path))

def encode_fields(fields: list[tuple[int, bytes]], *, flags: int=0, version: int=FRAME_VERSION, declared_body_length: int | None=None, declared_field_count: int | None=None, bad_crc: bool=False, trailing: bytes=b'') -> bytes:
    body = b''.join((_FIELD_HEADER.pack(field_id, 0, len(value)) + value for field_id, value in fields))
    header = _FRAME_HEADER.pack(FRAME_MAGIC, version, flags, len(fields) if declared_field_count is None else declared_field_count, len(body) if declared_body_length is None else declared_body_length)
    crc = zlib.crc32(header + body) & 4294967295
    if bad_crc:
        crc ^= 2779096485
    return header + body + struct.pack('>I', crc) + trailing

def decode_fields(frame: bytes, mark: Guard | None=None) -> tuple[tuple[int, bytes], ...]:
    _mark(mark, 'frame.size')
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError('frame_too_large')
    _mark(mark, 'frame.header')
    if len(frame) < _FRAME_HEADER.size + 4:
        raise ProtocolError('frame_truncated')
    magic, version, flags, field_count, body_length = _FRAME_HEADER.unpack_from(frame)
    _mark(mark, 'frame.magic')
    if magic != FRAME_MAGIC:
        raise ProtocolError('frame_magic')
    _mark(mark, 'frame.version')
    if version != FRAME_VERSION or flags != 0:
        raise ProtocolError('frame_version_or_flags')
    _mark(mark, 'frame.body_length')
    if body_length > MAX_TLV_BYTES or len(frame) != _FRAME_HEADER.size + body_length + 4:
        raise ProtocolError('frame_body_length')
    _mark(mark, 'frame.field_count')
    if field_count > MAX_FIELD_COUNT:
        raise ProtocolError('frame_field_count')
    expected_crc = struct.unpack_from('>I', frame, len(frame) - 4)[0]
    actual_crc = zlib.crc32(frame[:-4]) & 4294967295
    if expected_crc != actual_crc:
        raise ProtocolError('frame_crc')
    offset = _FRAME_HEADER.size
    body_end = offset + body_length
    rows: list[tuple[int, bytes]] = []
    seen: set[int] = set()
    previous = -1
    while offset < body_end:
        if offset + _FIELD_HEADER.size > body_end:
            raise ProtocolError('field_header_truncated')
        field_id, field_flags, length = _FIELD_HEADER.unpack_from(frame, offset)
        offset += _FIELD_HEADER.size
        if field_flags != 0 or length > MAX_TLV_BYTES or offset + length > body_end:
            raise ProtocolError('field_length_or_flags')
        _mark(mark, 'frame.field_order')
        if field_id <= previous:
            raise ProtocolError('field_order')
        previous = field_id
        _mark(mark, 'frame.field_unique')
        if field_id in seen:
            raise ProtocolError('field_duplicate')
        seen.add(field_id)
        _mark(mark, 'frame.field_known')
        if field_id not in FIELD_NAMES:
            raise ProtocolError('field_unknown')
        rows.append((field_id, frame[offset:offset + length]))
        offset += length
    if offset != body_end or len(rows) != field_count:
        raise ProtocolError('field_count_mismatch')
    _mark(mark, 'frame.required_fields')
    if tuple((field_id for field_id, _ in rows)) != REQUIRED_FIELDS:
        raise ProtocolError('required_fields')
    return tuple(rows)

def encode_response(response: Response) -> bytes:
    fields = [(FIELD_QUERY, encode_query(response.q)), (FIELD_META, encode_meta(response.root, response.meta)), (FIELD_PAYLOAD, encode_payload(response.payload)), (FIELD_WITNESS, encode_witness(response.meta.mode, response.meta.aux_ref, response.witness, response.meta.n)), (FIELD_ANCHOR, encode_anchor(response.certificate))]
    return encode_fields(fields)

def decode_response(frame: bytes, mark: Guard | None=None) -> ParsedFrame:
    rows = decode_fields(frame, mark)
    mapping = dict(rows)
    q = decode_query(mapping[FIELD_QUERY], mark)
    root, meta = decode_meta(mapping[FIELD_META], mark)
    payload = decode_payload(mapping[FIELD_PAYLOAD], mark)
    witness_mode, witness_aux_ref, witness, _n_prime = decode_witness(mapping[FIELD_WITNESS], mark)
    if witness_mode != meta.mode or witness_aux_ref != meta.aux_ref:
        raise ProtocolError('witness_meta_mismatch')
    certificate = decode_anchor(mapping[FIELD_ANCHOR], mark)
    return ParsedFrame(q=q, root=root, meta=meta, payload=payload, witness=witness, certificate=certificate, raw_fields=rows)

def replace_field(frame: bytes, field_id: int, value: bytes) -> bytes:
    fields = list(decode_fields(frame))
    replaced = False
    output: list[tuple[int, bytes]] = []
    for current, data in fields:
        if current == field_id:
            output.append((current, value))
            replaced = True
        else:
            output.append((current, data))
    if not replaced:
        raise KeyError(field_id)
    return encode_fields(output)

def field_map(frame: bytes) -> dict[int, bytes]:
    return dict(decode_fields(frame))

def frame_size_breakdown(frame: bytes) -> dict[str, int]:
    fields = decode_fields(frame)
    result = {FIELD_NAMES[field_id]: len(data) for field_id, data in fields}
    result['framing'] = len(frame) - sum(result.values())
    result['total'] = len(frame)
    return result

def raw_tlvs(frame: bytes) -> tuple[tuple[int, int, int, bytes], ...]:
    rows: list[tuple[int, int, int, bytes]] = []
    if len(frame) < _FRAME_HEADER.size + 4:
        return tuple()
    try:
        _magic, _version, _flags, _count, body_length = _FRAME_HEADER.unpack_from(frame)
    except struct.error:
        return tuple()
    body_end = min(len(frame) - 4, _FRAME_HEADER.size + min(body_length, MAX_TLV_BYTES))
    offset = _FRAME_HEADER.size
    while offset + _FIELD_HEADER.size <= body_end and len(rows) < MAX_FIELD_COUNT:
        header_offset = offset
        try:
            field_id, _flags, length = _FIELD_HEADER.unpack_from(frame, offset)
        except struct.error:
            break
        offset += _FIELD_HEADER.size
        if length > MAX_TLV_BYTES or offset + length > body_end:
            break
        value = frame[offset:offset + length]
        rows.append((field_id, header_offset, length, value))
        offset += length
    return tuple(rows)
