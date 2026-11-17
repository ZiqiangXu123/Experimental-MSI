from __future__ import annotations
import struct
from dataclasses import dataclass
from .constants import CANONICAL_HEADER_BYTES, DIGEST_BYTES, INTERNAL_TAG, LEAF_TAG, NOCTX_LEAF_TAG
from .crypto import OperationCounter, sha256
from .models import MerklePath, Query
from .util import is_power_of_two, next_power_of_two

@dataclass(frozen=True)
class MerkleMaterial:
    root: bytes
    levels: tuple[tuple[bytes, ...], ...]

    @property
    def padded_leaves(self) -> tuple[bytes, ...]:
        return self.levels[0]

    @property
    def depth(self) -> int:
        return len(self.levels) - 1

def leaf_hash(q: Query, block: bytes, counter: OperationCounter | None=None, *, context_bound: bool=True) -> bytes:
    if context_bound:
        message = LEAF_TAG + struct.pack('>IQI', q[0], q[1], q[2]) + block
    else:
        body = block[CANONICAL_HEADER_BYTES:] if len(block) >= CANONICAL_HEADER_BYTES else block
        message = NOCTX_LEAF_TAG + body
    return sha256(message, counter)

def internal_hash(left: bytes, right: bytes, counter: OperationCounter | None=None) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('Merkle children must be 32-byte digests')
    return sha256(INTERNAL_TAG + left + right, counter)

def build_merkle(leaves: list[bytes]) -> MerkleMaterial:
    if not leaves:
        raise ValueError('empty epochs are excluded')
    if any((len(digest) != DIGEST_BYTES for digest in leaves)):
        raise ValueError('leaf digest has incorrect width')
    padded = list(leaves)
    padded.extend([leaves[-1]] * (next_power_of_two(len(leaves)) - len(leaves)))
    levels: list[tuple[bytes, ...]] = [tuple(padded)]
    current = padded
    while len(current) > 1:
        nxt = [internal_hash(current[i], current[i + 1]) for i in range(0, len(current), 2)]
        levels.append(tuple(nxt))
        current = nxt
    return MerkleMaterial(levels=tuple(levels), root=levels[-1][0])

def extract_path(levels: tuple[tuple[bytes, ...], ...], position: int) -> MerklePath:
    n_prime = len(levels[0])
    if not 1 <= position <= n_prime:
        raise ValueError('position outside padded tree')
    idx = position - 1
    path: list[tuple[bytes, int]] = []
    for level in levels[:-1]:
        if idx % 2 == 0:
            sibling, direction = (idx + 1, 0)
        else:
            sibling, direction = (idx - 1, 1)
        path.append((level[sibling], direction))
        idx //= 2
    return tuple(path)

def rebuild_path(position: int, vector: tuple[bytes, ...], counter: OperationCounter | None=None) -> MerklePath | None:
    if not 1 <= position <= len(vector) or not is_power_of_two(len(vector)):
        return None
    if any((len(d) != DIGEST_BYTES for d in vector)):
        return None
    idx = position - 1
    current: tuple[bytes, ...] | list[bytes] = vector
    path: list[tuple[bytes, int]] = []
    while len(current) > 1:
        if idx % 2 == 0:
            sibling, direction = (idx + 1, 0)
        else:
            sibling, direction = (idx - 1, 1)
        path.append((current[sibling], direction))
        current = [internal_hash(current[j], current[j + 1], counter) for j in range(0, len(current), 2)]
        idx //= 2
    return tuple(path)

def verify_member(q: Query, block: bytes, path: MerklePath, root: bytes, n: int, counter: OperationCounter | None=None, *, context_bound: bool=True) -> tuple[bool, str]:
    if not 1 <= q[2] <= n:
        return (False, 'position_out_of_range')
    expected_depth = next_power_of_two(n).bit_length() - 1
    if len(path) != expected_depth:
        return (False, 'wrong_path_depth')
    running = leaf_hash(q, block, counter, context_bound=context_bound)
    for sibling, direction in path:
        if len(sibling) != DIGEST_BYTES:
            return (False, 'wrong_sibling_width')
        if direction not in (0, 1):
            return (False, 'invalid_direction')
        running = internal_hash(running, sibling, counter) if direction == 0 else internal_hash(sibling, running, counter)
    return (running == root, 'ok' if running == root else 'root_mismatch')
