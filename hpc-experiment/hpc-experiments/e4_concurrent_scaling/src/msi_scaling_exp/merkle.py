from __future__ import annotations
import hashlib, struct
from typing import Sequence
LEAF_TAG = b'MSI-E4-LEAF\x00'
NODE_TAG = b'MSI-E4-NODE\x00'
SIBLING_TAG = b'MSI-E4-SIBLING\x00'
FILL_LEAF_TAG = b'MSI-E4-FILL-LEAF\x00'
PAYLOAD_TAG = b'MSI-E4-BLOCK\x00'
ROOT_STMT_TAG = b'MSI-E4-ROOT-STMT\x00'

def sha256(d: bytes) -> bytes:
    return hashlib.sha256(d).digest()

def canonical_payload(shard: int, epoch: int, block_bytes: int, seed: int, position: int=0) -> bytes:
    if block_bytes < 48:
        raise ValueError('block size')
    h = struct.pack('>12sQQIQ', PAYLOAD_TAG[:12], shard, epoch, position, seed & (1 << 64) - 1)
    n = block_bytes - len(h)
    d = sha256(PAYLOAD_TAG + struct.pack('>QQIQ', shard, epoch, position, seed & (1 << 64) - 1))
    return h + (d * ((n + 31) // 32))[:n]

def check_canonical_payload(p: bytes, s: int, t: int, b: int, seed: int, i: int=0) -> bool:
    return len(p) == b and p == canonical_payload(s, t, b, seed, i)

def leaf_hash(s: int, t: int, i: int, p: bytes) -> bytes:
    return sha256(LEAF_TAG + struct.pack('>QQQ', s, t, i) + len(p).to_bytes(8, 'big') + p)

def sibling_digest(s: int, t: int, l: int, seed: int) -> bytes:
    return sha256(SIBLING_TAG + struct.pack('>QQIQ', s, t, l, seed & (1 << 64) - 1))

def path_fixture(s: int, t: int, depth: int, seed: int):
    return tuple(((sibling_digest(s, t, l, seed), 1) for l in range(depth)))

def apply_path(leaf: bytes, path: Sequence[tuple[bytes, int]]) -> bytes:
    cur = leaf
    for sib, right in path:
        if len(sib) != 32 or right not in (0, 1):
            raise ValueError('path')
        cur = sha256(NODE_TAG + (cur + sib if right else sib + cur))
    return cur

def fixture_root(s: int, t: int, n: int, b: int, seed: int) -> bytes:
    p = canonical_payload(s, t, b, seed, 0)
    return apply_path(leaf_hash(s, t, 0, p), path_fixture(s, t, n.bit_length() - 1, seed))

def _fill_leaf(s: int, t: int, i: int, seed: int) -> bytes:
    return sha256(FILL_LEAF_TAG + struct.pack('>QQIQ', s, t, i, seed & (1 << 64) - 1))

def leaf_vector(s: int, t: int, n: int, b: int, seed: int):
    p = canonical_payload(s, t, b, seed, 0)
    return (leaf_hash(s, t, 0, p),) + tuple((_fill_leaf(s, t, i, seed) for i in range(1, n)))

def build_root(leaves: Sequence[bytes]) -> bytes:
    if not leaves or len(leaves) & len(leaves) - 1:
        raise ValueError('leaf count')
    layer = list(leaves)
    while len(layer) > 1:
        layer = [sha256(NODE_TAG + layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]

def path_from_leaves(pos: int, leaves: Sequence[bytes]):
    if pos < 0 or pos >= len(leaves) or (not leaves) or len(leaves) & len(leaves) - 1:
        raise ValueError('position/leaves')
    idx = pos
    layer = list(leaves)
    path = []
    while len(layer) > 1:
        j = idx ^ 1
        path.append((layer[j], 1 if j > idx else 0))
        layer = [sha256(NODE_TAG + layer[k] + layer[k + 1]) for k in range(0, len(layer), 2)]
        idx //= 2
    return tuple(path)

def hot_fixture(s: int, t: int, n: int, b: int, seed: int):
    p = canonical_payload(s, t, b, seed, 0)
    leaves = leaf_vector(s, t, n, b, seed)
    path = path_from_leaves(0, leaves)
    root = build_root(leaves)
    return (p, path, leaves, root)

def verify_member(s: int, t: int, i: int, p: bytes, path, root: bytes) -> bool:
    return apply_path(leaf_hash(s, t, i, p), path) == root

def root_statement(s: int, t: int, n: int, root: bytes) -> bytes:
    return ROOT_STMT_TAG + struct.pack('>QQI', s, t, n) + root
