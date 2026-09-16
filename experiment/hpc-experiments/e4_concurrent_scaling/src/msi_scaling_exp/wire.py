from __future__ import annotations
import json, socket, struct, zlib
from dataclasses import dataclass
from typing import Any, Sequence
REQ_MAGIC = b'E4RQ'
RESP_MAGIC = b'E4RS'
VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024
KIND_PING = 0
KIND_QUERY = 1
KIND_STATS = 2
KIND_SHUTDOWN = 3
KIND_RESET = 4
MODE_NONE = 0
MODE_FULL = 1
MODE_LEAF = 2
MODE_EXT = 3
MODE_NAME_TO_CODE = {'full': 1, 'leaf': 2, 'ext': 3}
MODE_CODE_TO_NAME = {v: k for k, v in MODE_NAME_TO_CODE.items()}
STATUS_OK = 0
STATUS_BAD_REQUEST = 1
STATUS_INTERNAL_ERROR = 2
STATUS_UNAVAILABLE = 3
REQ_HEADER = struct.Struct('>4sBBHQQQ')
RESP_HEADER = struct.Struct('>4sBBBBQQQIQQIIH32s')
CRC = struct.Struct('>I')
LENGTH = struct.Struct('>I')

@dataclass(frozen=True)
class Request:
    kind: int
    mode: int
    query_id: int
    shard: int = 0
    epoch: int = 0
    flags: int = 0

@dataclass(frozen=True)
class Response:
    kind: int
    status: int
    mode: int
    query_id: int
    shard: int
    epoch: int
    n: int
    payload_ref: int
    aux_ref: int
    root: bytes
    payload: bytes
    witness: bytes
    signature: bytes

    @property
    def total_body_bytes(self):
        return RESP_HEADER.size + len(self.payload) + len(self.witness) + len(self.signature) + 4

def _crc(body):
    return body + CRC.pack(zlib.crc32(body) & 4294967295)

def _check(frame):
    if len(frame) < 4:
        raise ValueError('truncated')
    b, c = (frame[:-4], CRC.unpack(frame[-4:])[0])
    if zlib.crc32(b) & 4294967295 != c:
        raise ValueError('CRC')
    return b

def encode_request(r: Request) -> bytes:
    return _crc(REQ_HEADER.pack(REQ_MAGIC, VERSION, r.kind, (r.mode & 255) << 8 | r.flags & 255, r.query_id, r.shard, r.epoch))

def decode_request(frame: bytes) -> Request:
    b = _check(frame)
    if len(b) != REQ_HEADER.size:
        raise ValueError('request length')
    magic, v, k, p, q, s, t = REQ_HEADER.unpack(b)
    if magic != REQ_MAGIC or v != VERSION:
        raise ValueError('request version')
    return Request(k, p >> 8 & 255, q, s, t, p & 255)

def encode_path(path: Sequence[tuple[bytes, int]]) -> bytes:
    out = bytearray()
    for d, r in path:
        if len(d) != 32 or r not in (0, 1):
            raise ValueError('path')
        out.extend(d)
        out.append(r)
    return bytes(out)

def decode_path(raw: bytes, depth: int):
    if len(raw) != depth * 33:
        raise ValueError('path length')
    return tuple(((raw[i:i + 32], raw[i + 32]) for i in range(0, len(raw), 33)))

def encode_leaf_vector(v: Sequence[bytes]) -> bytes:
    if any((len(x) != 32 for x in v)):
        raise ValueError('leaf')
    return b''.join(v)

def decode_leaf_vector(raw: bytes, n: int):
    if len(raw) != n * 32:
        raise ValueError('leaf length')
    return tuple((raw[i:i + 32] for i in range(0, len(raw), 32)))

def encode_response(r: Response) -> bytes:
    h = RESP_HEADER.pack(RESP_MAGIC, VERSION, r.kind, r.status, r.mode, r.query_id, r.shard, r.epoch, r.n, r.payload_ref, r.aux_ref, len(r.payload), len(r.witness), len(r.signature), r.root)
    return _crc(h + r.payload + r.witness + r.signature)

def decode_response(frame: bytes) -> Response:
    b = _check(frame)
    if len(b) < RESP_HEADER.size:
        raise ValueError('response header')
    m, v, k, st, mode, q, s, t, n, p, a, pl, wl, sl, root = RESP_HEADER.unpack_from(b)
    if m != RESP_MAGIC or v != VERSION:
        raise ValueError('response version')
    if len(b) != RESP_HEADER.size + pl + wl + sl:
        raise ValueError('response lengths')
    o = RESP_HEADER.size
    payload = b[o:o + pl]
    o += pl
    w = b[o:o + wl]
    o += wl
    sig = b[o:o + sl]
    return Response(k, st, mode, q, s, t, n, p, a, root, payload, w, sig)

def control_response(kind: int, qid: int, obj: Any, status: int=STATUS_OK) -> Response:
    return Response(kind, status, MODE_NONE, qid, 0, 0, 0, 0, 0, bytes(32), json.dumps(obj, sort_keys=True, separators=(',', ':')).encode(), b'', b'')

def decode_control_json(r: Response):
    x = json.loads(r.payload.decode())
    if r.status != STATUS_OK:
        raise RuntimeError(x)
    return x

def send_frame(sock: socket.socket, body: bytes) -> int:
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError('frame max')
    f = LENGTH.pack(len(body)) + body
    sock.sendall(f)
    return len(f)

def recv_exact(sock: socket.socket, n: int) -> bytes:
    b = bytearray(n)
    v = memoryview(b)
    o = 0
    while o < n:
        k = sock.recv_into(v[o:])
        if k == 0:
            raise EOFError('closed')
        o += k
    return bytes(b)

def recv_frame(sock: socket.socket) -> bytes:
    n = LENGTH.unpack(recv_exact(sock, 4))[0]
    if n > MAX_FRAME_BYTES:
        raise ValueError('incoming max')
    return recv_exact(sock, n)
