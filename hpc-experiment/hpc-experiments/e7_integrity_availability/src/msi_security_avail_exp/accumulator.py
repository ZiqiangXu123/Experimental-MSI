from __future__ import annotations
from dataclasses import dataclass
from .constants import ACC_INTERNAL_TAG, ACC_LEAF_TAG, DIGEST_BYTES
from .crypto import OperationCounter, sha256
from .models import MerklePath
from .util import next_power_of_two

@dataclass(frozen=True)
class AccumulatorMaterial:
    root: bytes
    levels: tuple[tuple[bytes, ...], ...]

def statement_hash(statement: bytes, counter: OperationCounter | None=None) -> bytes:
    return sha256(ACC_LEAF_TAG + len(statement).to_bytes(4, 'big') + statement, counter)

def node_hash(left: bytes, right: bytes, counter: OperationCounter | None=None) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('accumulator children must be 32 bytes')
    return sha256(ACC_INTERNAL_TAG + left + right, counter)

def build_accumulator(statements: list[bytes]) -> AccumulatorMaterial:
    if not statements:
        raise ValueError('accumulator must contain at least one statement')
    leaves = [statement_hash(item) for item in statements]
    leaves.extend([leaves[-1]] * (next_power_of_two(len(leaves)) - len(leaves)))
    levels: list[tuple[bytes, ...]] = [tuple(leaves)]
    current = leaves
    while len(current) > 1:
        nxt = [node_hash(current[i], current[i + 1]) for i in range(0, len(current), 2)]
        levels.append(tuple(nxt))
        current = nxt
    return AccumulatorMaterial(root=levels[-1][0], levels=tuple(levels))

def extract_accumulator_path(levels: tuple[tuple[bytes, ...], ...], position: int) -> MerklePath:
    if not 1 <= position <= len(levels[0]):
        raise ValueError('accumulator position out of range')
    index = position - 1
    path: list[tuple[bytes, int]] = []
    for level in levels[:-1]:
        if index % 2 == 0:
            sibling, direction = (index + 1, 0)
        else:
            sibling, direction = (index - 1, 1)
        path.append((level[sibling], direction))
        index //= 2
    return tuple(path)

def verify_accumulator(statement: bytes, position: int, k_star: int, path: MerklePath, expected_root: bytes, counter: OperationCounter | None=None) -> bool:
    if not 1 <= position <= k_star:
        return False
    expected_depth = next_power_of_two(k_star).bit_length() - 1
    if len(path) != expected_depth:
        return False
    running = statement_hash(statement, counter)
    for sibling, direction in path:
        if len(sibling) != DIGEST_BYTES or direction not in (0, 1):
            return False
        running = node_hash(running, sibling, counter) if direction == 0 else node_hash(sibling, running, counter)
    return running == expected_root
