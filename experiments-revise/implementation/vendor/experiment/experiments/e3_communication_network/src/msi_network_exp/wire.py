from __future__ import annotations
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any
from .constants import CODECS, DEPLOYMENTS, FRAME_HEADER_BYTES, LAYOUTS, MODE_BY_SCHEME, MSG_CONFIG, MSG_CONFIG_ACK, MSG_ERROR, MSG_PING, MSG_PONG, MSG_QUERY_COALESCED, MSG_QUERY_PAYLOAD, MSG_QUERY_WITNESS, MSG_RESPONSE_COALESCED, MSG_RESPONSE_PAYLOAD, MSG_RESPONSE_WITNESS, SCHEMES, WIRE_MAGIC, WIRE_VERSION
from .models import CandidatePayload, DirectCertificate, MSIEntry, Response
_FRAME = struct.Struct('>4sBBHII')
_QUERY = struct.Struct('>QIQIB')
_PING = struct.Struct('>Q')
_CONFIG = struct.Struct('>Q')
_META = struct.Struct('>IQIIQ32s32sB')
_PAYLOAD_FIXED = struct.Struct('>32sBBIIIQII')
_WITNESS_HEAD = struct.Struct('>BI')
_CERT_HEAD = struct.Struct('>32sH')
_COALESCED_HEAD = struct.Struct('>QIIII')
_PAYLOAD_RESP_HEAD = struct.Struct('>QIII')
_WITNESS_RESP_HEAD = struct.Struct('>QII')
MODE_IDS = {'full': 1, 'leaf': 2, 'ext': 3}
ID_MODES = {value: key for key, value in MODE_IDS.items()}
LAYOUT_IDS = {name: i + 1 for i, name in enumerate(LAYOUTS)}
ID_LAYOUTS = {value: key for key, value in LAYOUT_IDS.items()}
CODEC_IDS = {name: i + 1 for i, name in enumerate(CODECS)}
ID_CODECS = {value: key for key, value in CODEC_IDS.items()}
SCHEME_IDS = {name: i + 1 for i, name in enumerate(SCHEMES)}
ID_SCHEMES = {value: key for key, value in SCHEME_IDS.items()}
MAX_FRAME_BODY = 128 * 1024 * 1024

@dataclass(frozen=True)
class Frame:
    message_type: int
    flags: int
    body: bytes
    total_bytes: int

@dataclass(frozen=True)
class WireBreakdown:
    request_bytes: int = 0
    payload_bytes: int = 0
    witness_bytes: int = 0
    anchor_evidence_bytes: int = 0
    metadata_bytes: int = 0
    framing_bytes: int = 0
    total_response_bytes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {'request_bytes': self.request_bytes, 'payload_bytes': self.payload_bytes, 'witness_bytes': self.witness_bytes, 'anchor_evidence_bytes': self.anchor_evidence_bytes, 'metadata_bytes': self.metadata_bytes, 'framing_bytes': self.framing_bytes, 'total_response_bytes': self.total_response_bytes}

def encode_frame(message_type: int, body: bytes, flags: int=0) -> bytes:
    if len(body) > MAX_FRAME_BODY:
        raise ValueError(f'frame body exceeds {MAX_FRAME_BODY} bytes')
    crc = zlib.crc32(body) & 4294967295
    return _FRAME.pack(WIRE_MAGIC, WIRE_VERSION, message_type, flags, len(body), crc) + body

def decode_frame_bytes(raw: bytes) -> Frame:
    if len(raw) < FRAME_HEADER_BYTES:
        raise ValueError('truncated frame')
    magic, version, message_type, flags, body_len, expected_crc = _FRAME.unpack_from(raw)
    if magic != WIRE_MAGIC or version != WIRE_VERSION:
        raise ValueError('wire magic/version mismatch')
    if body_len > MAX_FRAME_BODY or len(raw) != FRAME_HEADER_BYTES + body_len:
        raise ValueError('frame byte length mismatch')
    body = raw[FRAME_HEADER_BYTES:]
    if zlib.crc32(body) & 4294967295 != expected_crc:
        raise ValueError('wire frame CRC32 mismatch')
    return Frame(message_type=message_type, flags=flags, body=body, total_bytes=len(raw))

def recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError(f'socket closed with {remaining} bytes remaining')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)

def recv_frame(sock: socket.socket) -> Frame:
    header = recv_exact(sock, FRAME_HEADER_BYTES)
    magic, version, message_type, flags, body_len, expected_crc = _FRAME.unpack(header)
    if magic != WIRE_MAGIC or version != WIRE_VERSION:
        raise ValueError('wire magic/version mismatch')
    if body_len > MAX_FRAME_BODY:
        raise ValueError('frame body length exceeds safety limit')
    body = recv_exact(sock, body_len)
    if zlib.crc32(body) & 4294967295 != expected_crc:
        raise ValueError('wire frame CRC32 mismatch')
    return Frame(message_type=message_type, flags=flags, body=body, total_bytes=FRAME_HEADER_BYTES + body_len)

def send_frame(sock: socket.socket, frame: bytes) -> None:
    sock.sendall(frame)

def encode_ping(request_id: int) -> bytes:
    return encode_frame(MSG_PING, _PING.pack(request_id))

def decode_ping(body: bytes) -> int:
    if len(body) != _PING.size:
        raise ValueError('invalid ping body')
    return int(_PING.unpack(body)[0])

def encode_pong(request_id: int) -> bytes:
    return encode_frame(MSG_PONG, _PING.pack(request_id))

def encode_config(baseline_rtt_ns: int) -> bytes:
    return encode_frame(MSG_CONFIG, _CONFIG.pack(max(0, int(baseline_rtt_ns))))

def decode_config(body: bytes) -> int:
    if len(body) != _CONFIG.size:
        raise ValueError('invalid config body')
    return int(_CONFIG.unpack(body)[0])

def encode_config_ack() -> bytes:
    return encode_frame(MSG_CONFIG_ACK, b'')

def encode_error(message: str) -> bytes:
    raw = message.encode('utf-8', errors='replace')[:4096]
    return encode_frame(MSG_ERROR, raw)

def encode_query(message_type: int, request_id: int, q: tuple[int, int, int], scheme: str) -> bytes:
    if message_type not in {MSG_QUERY_COALESCED, MSG_QUERY_PAYLOAD, MSG_QUERY_WITNESS}:
        raise ValueError('invalid query message type')
    try:
        scheme_id = SCHEME_IDS[scheme]
    except KeyError as exc:
        raise ValueError(f'unsupported scheme {scheme}') from exc
    shard, epoch, position = q
    return encode_frame(message_type, _QUERY.pack(request_id, shard, epoch, position, scheme_id))

def decode_query(body: bytes) -> tuple[int, tuple[int, int, int], str]:
    if len(body) != _QUERY.size:
        raise ValueError('invalid query body')
    request_id, shard, epoch, position, scheme_id = _QUERY.unpack(body)
    try:
        scheme = ID_SCHEMES[scheme_id]
    except KeyError as exc:
        raise ValueError('invalid scheme identifier') from exc
    return (int(request_id), (int(shard), int(epoch), int(position)), scheme)

def _encode_meta(entry: MSIEntry, response: Response) -> bytes:
    mode_id = MODE_IDS[response.mode]
    return _META.pack(response.q[0], response.q[1], response.q[2], entry.meta.n, entry.meta.k, entry.root, response.aux_ref, mode_id)

def _decode_meta(raw: bytes) -> dict[str, Any]:
    if len(raw) != _META.size:
        raise ValueError('invalid response metadata length')
    shard, epoch, position, n, k, root, aux_ref, mode_id = _META.unpack(raw)
    try:
        mode = ID_MODES[mode_id]
    except KeyError as exc:
        raise ValueError('invalid response mode') from exc
    return {'q': (int(shard), int(epoch), int(position)), 'n': int(n), 'k': int(k), 'root': root, 'aux_ref': aux_ref, 'mode': mode}

def _encode_payload(payload: CandidatePayload) -> bytes:
    try:
        layout_id = LAYOUT_IDS[payload.layout]
        codec_id = CODEC_IDS[payload.codec]
    except KeyError as exc:
        raise ValueError('unsupported layout or codec') from exc
    fixed = _PAYLOAD_FIXED.pack(payload.payload_ref, layout_id, codec_id, payload.version, payload.position, payload.canonical_length, payload.logical_offset, payload.encoded_crc32, len(payload.encoded))
    return fixed + payload.encoded

def _decode_payload(raw: bytes) -> CandidatePayload:
    if len(raw) < _PAYLOAD_FIXED.size:
        raise ValueError('truncated candidate payload')
    payload_ref, layout_id, codec_id, version, position, canonical_length, logical_offset, encoded_crc32, encoded_len = _PAYLOAD_FIXED.unpack_from(raw)
    encoded = raw[_PAYLOAD_FIXED.size:]
    if len(encoded) != encoded_len:
        raise ValueError('candidate payload encoded length mismatch')
    try:
        layout = ID_LAYOUTS[layout_id]
        codec = ID_CODECS[codec_id]
    except KeyError as exc:
        raise ValueError('invalid layout or codec identifier') from exc
    return CandidatePayload(payload_ref=payload_ref, layout=layout, codec=codec, version=int(version), position=int(position), encoded=encoded, canonical_length=int(canonical_length), logical_offset=int(logical_offset), encoded_crc32=int(encoded_crc32))

def _encode_witness(response: Response) -> bytes:
    mode_id = MODE_IDS[response.mode]
    if response.mode in {'full', 'ext'}:
        path = response.witness
        if not isinstance(path, tuple):
            raise ValueError('path witness must be a tuple')
        out = bytearray(_WITNESS_HEAD.pack(mode_id, len(path)))
        for digest, direction in path:
            if len(digest) != 32 or direction not in (0, 1):
                raise ValueError('invalid path element')
            out.extend(digest)
            out.append(direction)
        return bytes(out)
    if response.mode == 'leaf':
        vector = response.witness
        if not isinstance(vector, tuple):
            raise ValueError('leaf witness must be a tuple')
        out = bytearray(_WITNESS_HEAD.pack(mode_id, len(vector)))
        for digest in vector:
            if len(digest) != 32:
                raise ValueError('invalid leaf digest')
            out.extend(digest)
        return bytes(out)
    raise ValueError('unsupported response mode')

def _decode_witness(raw: bytes) -> tuple[str, tuple[Any, ...]]:
    if len(raw) < _WITNESS_HEAD.size:
        raise ValueError('truncated witness')
    mode_id, count = _WITNESS_HEAD.unpack_from(raw)
    try:
        mode = ID_MODES[mode_id]
    except KeyError as exc:
        raise ValueError('invalid witness mode') from exc
    data = memoryview(raw)[_WITNESS_HEAD.size:]
    if mode in {'full', 'ext'}:
        expected = int(count) * 33
        if len(data) != expected:
            raise ValueError('path witness length mismatch')
        path: list[tuple[bytes, int]] = []
        for i in range(int(count)):
            start = i * 33
            digest = bytes(data[start:start + 32])
            direction = int(data[start + 32])
            if direction not in (0, 1):
                raise ValueError('invalid path direction')
            path.append((digest, direction))
        return (mode, tuple(path))
    expected = int(count) * 32
    if len(data) != expected:
        raise ValueError('leaf-vector length mismatch')
    vector = tuple((bytes(data[i * 32:(i + 1) * 32]) for i in range(int(count))))
    return (mode, vector)

def _encode_certificate(cert: DirectCertificate) -> bytes:
    if len(cert.public_key) != 32 or len(cert.signature) != 64:
        raise ValueError('invalid direct certificate width')
    if len(cert.statement) > 65535:
        raise ValueError('certificate statement too long')
    return _CERT_HEAD.pack(cert.public_key, len(cert.statement)) + cert.statement + cert.signature

def _decode_certificate(raw: bytes) -> DirectCertificate:
    if len(raw) < _CERT_HEAD.size + 64:
        raise ValueError('truncated certificate')
    public_key, statement_len = _CERT_HEAD.unpack_from(raw)
    expected = _CERT_HEAD.size + int(statement_len) + 64
    if len(raw) != expected:
        raise ValueError('certificate length mismatch')
    statement = raw[_CERT_HEAD.size:_CERT_HEAD.size + statement_len]
    signature = raw[-64:]
    return DirectCertificate(public_key=public_key, statement=statement, signature=signature)

def _take_slices(body: bytes, offset: int, lengths: tuple[int, ...]) -> tuple[bytes, ...]:
    parts: list[bytes] = []
    cursor = offset
    for length in lengths:
        if length < 0 or cursor + length > len(body):
            raise ValueError('response component length outside body')
        parts.append(body[cursor:cursor + length])
        cursor += length
    if cursor != len(body):
        raise ValueError('response body contains trailing bytes')
    return tuple(parts)

def encode_coalesced_response(request_id: int, entry: MSIEntry, response: Response) -> tuple[bytes, WireBreakdown]:
    meta = _encode_meta(entry, response)
    payload = _encode_payload(response.payload)
    witness = _encode_witness(response)
    cert = _encode_certificate(response.certificate)
    body = _COALESCED_HEAD.pack(request_id, len(meta), len(payload), len(witness), len(cert))
    body += meta + payload + witness + cert
    frame = encode_frame(MSG_RESPONSE_COALESCED, body)
    overhead = len(frame) - len(meta) - len(payload) - len(witness) - len(cert)
    breakdown = WireBreakdown(payload_bytes=len(payload), witness_bytes=len(witness), anchor_evidence_bytes=len(cert), metadata_bytes=len(meta), framing_bytes=overhead, total_response_bytes=len(frame))
    return (frame, breakdown)

def decode_coalesced_response(frame: Frame) -> tuple[int, Response, dict[str, Any]]:
    if frame.message_type != MSG_RESPONSE_COALESCED or len(frame.body) < _COALESCED_HEAD.size:
        raise ValueError('not a coalesced response')
    request_id, lm, lp, lw, lc = _COALESCED_HEAD.unpack_from(frame.body)
    meta_raw, payload_raw, witness_raw, cert_raw = _take_slices(frame.body, _COALESCED_HEAD.size, (lm, lp, lw, lc))
    meta = _decode_meta(meta_raw)
    payload = _decode_payload(payload_raw)
    witness_mode, witness = _decode_witness(witness_raw)
    cert = _decode_certificate(cert_raw)
    if witness_mode != meta['mode']:
        raise ValueError('metadata/witness mode mismatch')
    response = Response(q=meta['q'], payload=payload, aux_ref=meta['aux_ref'], mode=meta['mode'], witness=witness, certificate=cert)
    return (int(request_id), response, meta)

def encode_payload_response(request_id: int, entry: MSIEntry, response: Response) -> tuple[bytes, WireBreakdown]:
    meta = _encode_meta(entry, response)
    payload = _encode_payload(response.payload)
    cert = _encode_certificate(response.certificate)
    body = _PAYLOAD_RESP_HEAD.pack(request_id, len(meta), len(payload), len(cert)) + meta + payload + cert
    frame = encode_frame(MSG_RESPONSE_PAYLOAD, body)
    overhead = len(frame) - len(meta) - len(payload) - len(cert)
    return (frame, WireBreakdown(payload_bytes=len(payload), anchor_evidence_bytes=len(cert), metadata_bytes=len(meta), framing_bytes=overhead, total_response_bytes=len(frame)))

def decode_payload_response(frame: Frame) -> tuple[int, dict[str, Any], CandidatePayload, DirectCertificate]:
    if frame.message_type != MSG_RESPONSE_PAYLOAD or len(frame.body) < _PAYLOAD_RESP_HEAD.size:
        raise ValueError('not a payload response')
    request_id, lm, lp, lc = _PAYLOAD_RESP_HEAD.unpack_from(frame.body)
    meta_raw, payload_raw, cert_raw = _take_slices(frame.body, _PAYLOAD_RESP_HEAD.size, (lm, lp, lc))
    return (int(request_id), _decode_meta(meta_raw), _decode_payload(payload_raw), _decode_certificate(cert_raw))

def encode_witness_response(request_id: int, entry: MSIEntry, response: Response) -> tuple[bytes, WireBreakdown]:
    meta = _encode_meta(entry, response)
    witness = _encode_witness(response)
    body = _WITNESS_RESP_HEAD.pack(request_id, len(meta), len(witness)) + meta + witness
    frame = encode_frame(MSG_RESPONSE_WITNESS, body)
    overhead = len(frame) - len(meta) - len(witness)
    return (frame, WireBreakdown(witness_bytes=len(witness), metadata_bytes=len(meta), framing_bytes=overhead, total_response_bytes=len(frame)))

def decode_witness_response(frame: Frame) -> tuple[int, dict[str, Any], tuple[Any, ...]]:
    if frame.message_type != MSG_RESPONSE_WITNESS or len(frame.body) < _WITNESS_RESP_HEAD.size:
        raise ValueError('not a witness response')
    request_id, lm, lw = _WITNESS_RESP_HEAD.unpack_from(frame.body)
    meta_raw, witness_raw = _take_slices(frame.body, _WITNESS_RESP_HEAD.size, (lm, lw))
    meta = _decode_meta(meta_raw)
    witness_mode, witness = _decode_witness(witness_raw)
    if witness_mode != meta['mode']:
        raise ValueError('metadata/witness mode mismatch')
    return (int(request_id), meta, witness)

def merge_split_response(payload_part: tuple[int, dict[str, Any], CandidatePayload, DirectCertificate], witness_part: tuple[int, dict[str, Any], tuple[Any, ...]]) -> tuple[int, Response, dict[str, Any]]:
    request_id, payload_meta, payload, cert = payload_part
    witness_id, witness_meta, witness = witness_part
    if request_id != witness_id:
        raise ValueError('split response request IDs differ')
    for field in ('q', 'n', 'k', 'root', 'aux_ref', 'mode'):
        if payload_meta[field] != witness_meta[field]:
            raise ValueError(f'split response metadata mismatch: {field}')
    return (request_id, Response(q=payload_meta['q'], payload=payload, aux_ref=payload_meta['aux_ref'], mode=payload_meta['mode'], witness=witness, certificate=cert), payload_meta)

def add_breakdowns(*items: WireBreakdown, request_bytes: int=0) -> WireBreakdown:
    return WireBreakdown(request_bytes=request_bytes + sum((item.request_bytes for item in items)), payload_bytes=sum((item.payload_bytes for item in items)), witness_bytes=sum((item.witness_bytes for item in items)), anchor_evidence_bytes=sum((item.anchor_evidence_bytes for item in items)), metadata_bytes=sum((item.metadata_bytes for item in items)), framing_bytes=sum((item.framing_bytes for item in items)), total_response_bytes=sum((item.total_response_bytes for item in items)))

def request_type_for(case: dict[str, Any], service_role: str) -> int:
    deployment = str(case.get('deployment', 'coalesced'))
    if deployment not in DEPLOYMENTS:
        raise ValueError('invalid deployment')
    if deployment == 'coalesced':
        if service_role != 'gateway':
            raise ValueError('coalesced deployment requires gateway service')
        return MSG_QUERY_COALESCED
    if service_role == 'payload':
        return MSG_QUERY_PAYLOAD
    if service_role == 'witness':
        return MSG_QUERY_WITNESS
    raise ValueError('split deployment requires payload or witness service')
