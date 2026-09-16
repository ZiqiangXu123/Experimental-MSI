from __future__ import annotations
import struct
import sys
from dataclasses import dataclass
from .constants import DIGEST_BYTES, INTERNAL_TAG, LEAF_TAG
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

def leaf_hash(q: Query, block: bytes, counter: OperationCounter | None=None) -> bytes:
    shard, epoch, position = q
    message = LEAF_TAG + struct.pack('>IQI', shard, epoch, position) + block
    return sha256(message, counter)

def internal_hash(left: bytes, right: bytes, counter: OperationCounter | None=None) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('Merkle children must be fixed-width digests')
    return sha256(INTERNAL_TAG + left + right, counter)

def build_merkle(leaves: list[bytes]) -> MerkleMaterial:
    if not leaves:
        raise ValueError('empty epochs are excluded')
    for digest in leaves:
        if len(digest) != DIGEST_BYTES:
            raise ValueError('leaf digest has the wrong width')
    padded = list(leaves)
    padded.extend([leaves[-1]] * (next_power_of_two(len(leaves)) - len(leaves)))
    levels: list[tuple[bytes, ...]] = [tuple(padded)]
    current = padded
    while len(current) > 1:
        nxt = [internal_hash(current[i], current[i + 1]) for i in range(0, len(current), 2)]
        levels.append(tuple(nxt))
        current = nxt
    return MerkleMaterial(root=levels[-1][0], levels=tuple(levels))

def extract_path(levels: tuple[tuple[bytes, ...], ...], position: int) -> MerklePath:
    n_prime = len(levels[0])
    if position < 1 or position > n_prime:
        raise ValueError('position outside padded leaf layer')
    idx = position - 1
    path: list[tuple[bytes, int]] = []
    for level in levels[:-1]:
        if idx % 2 == 0:
            sibling = idx + 1
            direction = 0
        else:
            sibling = idx - 1
            direction = 1
        path.append((level[sibling], direction))
        idx //= 2
    return tuple(path)

def rebuild_path(position: int, vector: tuple[bytes, ...], counter: OperationCounter | None=None) -> MerklePath | None:
    n_prime = len(vector)
    if position < 1 or position > n_prime or (not is_power_of_two(n_prime)):
        return None
    p = position - 1
    path: list[tuple[bytes, int]] = []
    if counter is not None:
        counter.record_allocation(path)
    current: tuple[bytes, ...] | list[bytes] = vector
    while len(current) > 1:
        if p % 2 == 0:
            sibling = p + 1
            direction = 0
        else:
            sibling = p - 1
            direction = 1
        item = (current[sibling], direction)
        path.append(item)
        if counter is not None:
            counter.record_allocation(item)
        nxt = [internal_hash(current[j], current[j + 1], counter) for j in range(0, len(current), 2)]
        if counter is not None:
            counter.record_allocation(nxt)
        current = nxt
        p //= 2
    result = tuple(path)
    if counter is not None:
        counter.record_allocation(result)
    return result

def verify_member(q: Query, block: bytes, path: MerklePath, root: bytes, n: int, counter: OperationCounter | None=None) -> bool:
    if q[2] < 1 or q[2] > n:
        return False
    expected_depth = next_power_of_two(n).bit_length() - 1
    if len(path) != expected_depth:
        return False
    running = leaf_hash(q, block, counter)
    for sibling, direction in path:
        if len(sibling) != DIGEST_BYTES or direction not in (0, 1):
            return False
        if direction == 0:
            running = internal_hash(running, sibling, counter)
        else:
            running = internal_hash(sibling, running, counter)
    return running == root
