from __future__ import annotations
import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Iterable, Sequence
from .crypto import Ed25519Signer, Ed25519Verifier
DIGEST_BYTES = 32
SIGNATURE_BYTES = 64
PUBLIC_KEY_BYTES = 32
TAG_STATEMENT_LEAF = b'MSI-E5-ACC-STMT\x00'
TAG_ACC_NODE = b'MSI-E5-ACC-NODE\x00'
TAG_ACC_BAG = b'MSI-E5-ACC-BAG\x00'
TAG_ACC_ROOT = b'MSI-E5-ACC-ROOT\x00'
TAG_DIRECT_CERT = b'MSI-E5-DIRECT-CERT\x00'
TAG_CHECKPOINT_CERT = b'MSI-E5-CHECKPOINT-CERT\x00'
TAG_EPOCH_ROOT = b'MSI-E5-EPOCH-ROOT\x00'
ROOT_STMT_STRUCT = struct.Struct('>4sIQQ32s')
ROOT_STMT_MAGIC = b'RST1'
DIRECT_EVIDENCE_HEADER = struct.Struct('>4sH')
DIRECT_EVIDENCE_MAGIC = b'E5D1'
AGG_EVIDENCE_HEADER = struct.Struct('>4sHQQBBH')
AGG_EVIDENCE_MAGIC = b'E5A1'
DIRECT_ANCHOR_STRUCT = struct.Struct('>4sHI32s')
DIRECT_ANCHOR_MAGIC = b'E5DR'
AGG_ANCHOR_STRUCT = struct.Struct('>4sHIQ32s64s32s')
AGG_ANCHOR_MAGIC = b'E5AR'
CHECKPOINT_OBJECT_STRUCT = struct.Struct('>4sIQ32s64s')
CHECKPOINT_OBJECT_MAGIC = b'E5CP'

class EvidenceError(ValueError):
    pass

@dataclass
class HashCounter:
    statement_leaf: int = 0
    internal_node: int = 0
    bag: int = 0
    final_root: int = 0

    @property
    def total(self) -> int:
        return self.statement_leaf + self.internal_node + self.bag + self.final_root

    def as_dict(self) -> dict[str, int]:
        return {'statement_leaf': self.statement_leaf, 'internal_node': self.internal_node, 'bag': self.bag, 'final_root': self.final_root, 'total': self.total}

@dataclass(frozen=True)
class RootStatement:
    shard: int
    epoch: int
    position: int
    root: bytes

    def __post_init__(self) -> None:
        if not 0 <= self.shard <= 4294967295:
            raise ValueError('shard is outside uint32 range')
        if not 1 <= self.epoch <= 18446744073709551615:
            raise ValueError('epoch must be a positive uint64')
        if not 1 <= self.position <= 18446744073709551615:
            raise ValueError('position must be a positive uint64')
        if len(self.root) != DIGEST_BYTES:
            raise ValueError('root must contain exactly 32 bytes')

    def encode(self) -> bytes:
        return ROOT_STMT_STRUCT.pack(ROOT_STMT_MAGIC, self.shard, self.epoch, self.position, self.root)

    @classmethod
    def decode(cls, encoded: bytes) -> 'RootStatement':
        if len(encoded) != ROOT_STMT_STRUCT.size:
            raise EvidenceError(f'RootStmt length {len(encoded)} is invalid')
        magic, shard, epoch, position, root = ROOT_STMT_STRUCT.unpack(encoded)
        if magic != ROOT_STMT_MAGIC:
            raise EvidenceError('RootStmt magic is invalid')
        return cls(shard=shard, epoch=epoch, position=position, root=root)

def deterministic_epoch_root(seed: int, shard: int, epoch: int) -> bytes:
    return hashlib.sha256(TAG_EPOCH_ROOT + int(seed).to_bytes(8, 'big', signed=False) + int(shard).to_bytes(4, 'big', signed=False) + int(epoch).to_bytes(8, 'big', signed=False)).digest()

def deterministic_statement(seed: int, shard: int, position: int) -> RootStatement:
    return RootStatement(shard=shard, epoch=position, position=position, root=deterministic_epoch_root(seed, shard, position))

def _hash(tag: bytes, parts: Iterable[bytes]) -> bytes:
    digest = hashlib.sha256()
    digest.update(tag)
    for part in parts:
        digest.update(len(part).to_bytes(4, 'big'))
        digest.update(part)
    return digest.digest()

def statement_leaf_hash(statement: RootStatement, counter: HashCounter | None=None) -> bytes:
    if counter is not None:
        counter.statement_leaf += 1
    return _hash(TAG_STATEMENT_LEAF, (statement.encode(),))

def acc_node_hash(left: bytes, right: bytes, counter: HashCounter | None=None) -> bytes:
    if len(left) != DIGEST_BYTES or len(right) != DIGEST_BYTES:
        raise ValueError('accumulator children must be 32-byte digests')
    if counter is not None:
        counter.internal_node += 1
    return _hash(TAG_ACC_NODE, (left, right))

def acc_bag_hash(left_peak: bytes, right_bag: bytes, counter: HashCounter | None=None) -> bytes:
    if len(left_peak) != DIGEST_BYTES or len(right_bag) != DIGEST_BYTES:
        raise ValueError('bag inputs must be 32-byte digests')
    if counter is not None:
        counter.bag += 1
    return _hash(TAG_ACC_BAG, (left_peak, right_bag))

def acc_final_root(count: int, bagged: bytes, counter: HashCounter | None=None) -> bytes:
    if count <= 0:
        raise ValueError('an accumulator root requires a non-empty prefix')
    if len(bagged) != DIGEST_BYTES:
        raise ValueError('bagged peak root must contain 32 bytes')
    if counter is not None:
        counter.final_root += 1
    return _hash(TAG_ACC_ROOT, (int(count).to_bytes(8, 'big'), bagged))

def peak_layout(count: int) -> list[tuple[int, int, int]]:
    if count <= 0:
        raise ValueError('count must be positive')
    output: list[tuple[int, int, int]] = []
    start = 0
    for height in range(count.bit_length() - 1, -1, -1):
        size = 1 << height
        if count & size:
            output.append((height, start, start + size))
            start += size
    if start != count:
        raise AssertionError('peak layout does not cover prefix')
    return output

def bag_peaks(peaks: Sequence[bytes], counter: HashCounter | None=None) -> bytes:
    if not peaks:
        raise ValueError('at least one peak is required')
    result = peaks[-1]
    for peak in reversed(peaks[:-1]):
        result = acc_bag_hash(peak, result, counter)
    return result

@dataclass(frozen=True)
class AccumulatorProof:
    prefix_count: int
    position: int
    path_siblings: tuple[bytes, ...]
    sibling_on_left: tuple[bool, ...]
    other_peaks: tuple[tuple[int, bytes], ...]

    def __post_init__(self) -> None:
        if self.prefix_count <= 0:
            raise ValueError('prefix_count must be positive')
        if not 1 <= self.position <= self.prefix_count:
            raise ValueError('position is outside the certified prefix')
        if len(self.path_siblings) != len(self.sibling_on_left):
            raise ValueError('path sibling and direction lengths differ')
        if len(self.path_siblings) > 255 or len(self.other_peaks) > 255:
            raise ValueError('proof exceeds serialization limits')
        for digest in self.path_siblings:
            if len(digest) != DIGEST_BYTES:
                raise ValueError('path sibling is not a 32-byte digest')
        for height, digest in self.other_peaks:
            if not 0 <= height <= 255 or len(digest) != DIGEST_BYTES:
                raise ValueError('invalid other-peak record')

    @property
    def direction_bytes(self) -> bytes:
        length = (len(self.sibling_on_left) + 7) // 8
        output = bytearray(length)
        for index, value in enumerate(self.sibling_on_left):
            if value:
                output[index // 8] |= 1 << index % 8
        return bytes(output)

    @property
    def expected_hash_calls(self) -> int:
        return len(self.path_siblings) + len(self.other_peaks) + 2

class DynamicAccumulator:

    def __init__(self) -> None:
        self.levels: list[list[bytes]] = []
        self.count = 0

    def append_leaf(self, leaf: bytes, counter: HashCounter | None=None) -> None:
        if len(leaf) != DIGEST_BYTES:
            raise ValueError('leaf must contain exactly 32 bytes')
        if not self.levels:
            self.levels.append([])
        self.levels[0].append(leaf)
        level = 0
        while len(self.levels[level]) % 2 == 0:
            left = self.levels[level][-2]
            right = self.levels[level][-1]
            parent = acc_node_hash(left, right, counter)
            level += 1
            if len(self.levels) <= level:
                self.levels.append([])
            self.levels[level].append(parent)
        self.count += 1

    def append_statement(self, statement: RootStatement, counter: HashCounter | None=None) -> None:
        self.append_leaf(statement_leaf_hash(statement, counter), counter)

    def peaks(self, prefix_count: int | None=None) -> list[bytes]:
        count = self.count if prefix_count is None else int(prefix_count)
        if not 1 <= count <= self.count:
            raise ValueError('prefix_count is outside retained history')
        output: list[bytes] = []
        for height, start, _ in peak_layout(count):
            index = start >> height
            try:
                output.append(self.levels[height][index])
            except IndexError as exc:
                raise RuntimeError('accumulator node store is incomplete') from exc
        return output

    def root(self, prefix_count: int | None=None, counter: HashCounter | None=None) -> bytes:
        count = self.count if prefix_count is None else int(prefix_count)
        return acc_final_root(count, bag_peaks(self.peaks(count), counter), counter)

    def proof(self, position: int, prefix_count: int | None=None) -> AccumulatorProof:
        count = self.count if prefix_count is None else int(prefix_count)
        if not 1 <= position <= count <= self.count:
            raise ValueError('invalid proof position or prefix')
        zero_based = position - 1
        layout = peak_layout(count)
        target_index = -1
        target_height = -1
        for index, (height, start, end) in enumerate(layout):
            if start <= zero_based < end:
                target_index = index
                target_height = height
                break
        if target_index < 0:
            raise AssertionError('target position is not covered by a peak')
        siblings: list[bytes] = []
        directions: list[bool] = []
        for level in range(target_height):
            node_index = zero_based >> level
            sibling_index = node_index ^ 1
            siblings.append(self.levels[level][sibling_index])
            directions.append(sibling_index < node_index)
        other_peaks: list[tuple[int, bytes]] = []
        for index, (height, start, _) in enumerate(layout):
            if index == target_index:
                continue
            other_peaks.append((height, self.levels[height][start >> height]))
        return AccumulatorProof(prefix_count=count, position=position, path_siblings=tuple(siblings), sibling_on_left=tuple(directions), other_peaks=tuple(other_peaks))

class FrontierAccumulator:

    def __init__(self, count: int=0, frontier: Sequence[bytes | None] | None=None):
        self.count = int(count)
        self.frontier = list(frontier or [])

    def clone(self) -> 'FrontierAccumulator':
        return FrontierAccumulator(self.count, self.frontier)

    def append_leaf(self, leaf: bytes, counter: HashCounter | None=None) -> None:
        if len(leaf) != DIGEST_BYTES:
            raise ValueError('leaf must contain 32 bytes')
        node = leaf
        height = 0
        while True:
            if len(self.frontier) <= height:
                self.frontier.extend([None] * (height + 1 - len(self.frontier)))
            existing = self.frontier[height]
            if existing is None:
                self.frontier[height] = node
                break
            node = acc_node_hash(existing, node, counter)
            self.frontier[height] = None
            height += 1
        self.count += 1

    def append_statement(self, statement: RootStatement, counter: HashCounter | None=None) -> None:
        self.append_leaf(statement_leaf_hash(statement, counter), counter)

    def peaks(self) -> list[bytes]:
        if self.count <= 0:
            raise ValueError('frontier is empty')
        output: list[bytes] = []
        for height, _, _ in peak_layout(self.count):
            try:
                value = self.frontier[height]
            except IndexError as exc:
                raise RuntimeError('frontier is incomplete') from exc
            if value is None:
                raise RuntimeError('frontier is inconsistent with its count')
            output.append(value)
        return output

    def root(self, counter: HashCounter | None=None) -> bytes:
        return acc_final_root(self.count, bag_peaks(self.peaks(), counter), counter)

@dataclass(frozen=True)
class DirectAnchor:
    shard: int
    public_key: bytes

    def serialize(self) -> bytes:
        if len(self.public_key) != PUBLIC_KEY_BYTES:
            raise ValueError('public key length is invalid')
        return DIRECT_ANCHOR_STRUCT.pack(DIRECT_ANCHOR_MAGIC, 1, self.shard, self.public_key)

@dataclass(frozen=True)
class AggregateAnchor:
    shard: int
    prefix_count: int
    accumulator_root: bytes
    signature: bytes
    public_key: bytes

    def serialize(self) -> bytes:
        if len(self.accumulator_root) != DIGEST_BYTES:
            raise ValueError('accumulator root length is invalid')
        if len(self.signature) != SIGNATURE_BYTES:
            raise ValueError('checkpoint signature length is invalid')
        if len(self.public_key) != PUBLIC_KEY_BYTES:
            raise ValueError('public key length is invalid')
        return AGG_ANCHOR_STRUCT.pack(AGG_ANCHOR_MAGIC, 1, self.shard, self.prefix_count, self.accumulator_root, self.signature, self.public_key)

    def checkpoint_object(self) -> bytes:
        return CHECKPOINT_OBJECT_STRUCT.pack(CHECKPOINT_OBJECT_MAGIC, self.shard, self.prefix_count, self.accumulator_root, self.signature)

def direct_cert_message(statement: RootStatement) -> bytes:
    return TAG_DIRECT_CERT + statement.encode()

def checkpoint_cert_message(shard: int, prefix_count: int, accumulator_root: bytes) -> bytes:
    if len(accumulator_root) != DIGEST_BYTES:
        raise ValueError('accumulator root must contain 32 bytes')
    return TAG_CHECKPOINT_CERT + int(shard).to_bytes(4, 'big') + int(prefix_count).to_bytes(8, 'big') + accumulator_root

def make_direct_evidence(statement: RootStatement, signer: Ed25519Signer) -> bytes:
    signature = signer.sign(direct_cert_message(statement))
    encoded = statement.encode()
    return DIRECT_EVIDENCE_HEADER.pack(DIRECT_EVIDENCE_MAGIC, len(encoded)) + encoded + signature

def parse_direct_evidence(evidence: bytes) -> tuple[RootStatement, bytes]:
    if len(evidence) < DIRECT_EVIDENCE_HEADER.size:
        raise EvidenceError('direct evidence is truncated')
    magic, statement_length = DIRECT_EVIDENCE_HEADER.unpack_from(evidence, 0)
    if magic != DIRECT_EVIDENCE_MAGIC:
        raise EvidenceError('direct evidence magic is invalid')
    expected = DIRECT_EVIDENCE_HEADER.size + statement_length + SIGNATURE_BYTES
    if len(evidence) != expected:
        raise EvidenceError('direct evidence length is inconsistent')
    start = DIRECT_EVIDENCE_HEADER.size
    statement = RootStatement.decode(evidence[start:start + statement_length])
    signature = evidence[start + statement_length:]
    return (statement, signature)

def verify_direct_evidence(expected: RootStatement, evidence: bytes, anchor: DirectAnchor, verifier: Ed25519Verifier) -> bool:
    if expected.shard != anchor.shard:
        return False
    try:
        statement, signature = parse_direct_evidence(evidence)
    except (EvidenceError, ValueError, struct.error):
        return False
    if statement != expected:
        return False
    return verifier.verify(direct_cert_message(statement), signature)

def make_checkpoint_anchor(shard: int, prefix_count: int, accumulator_root: bytes, signer: Ed25519Signer) -> AggregateAnchor:
    signature = signer.sign(checkpoint_cert_message(shard, prefix_count, accumulator_root))
    return AggregateAnchor(shard=shard, prefix_count=prefix_count, accumulator_root=accumulator_root, signature=signature, public_key=signer.public_key)

def make_aggregate_evidence(statement: RootStatement, proof: AccumulatorProof) -> bytes:
    if statement.position != proof.position:
        raise ValueError('RootStmt position and proof position differ')
    encoded_statement = statement.encode()
    directions = proof.direction_bytes
    header = AGG_EVIDENCE_HEADER.pack(AGG_EVIDENCE_MAGIC, len(encoded_statement), proof.prefix_count, proof.position, len(proof.path_siblings), len(proof.other_peaks), len(directions))
    body = bytearray(encoded_statement)
    body.extend(directions)
    for sibling in proof.path_siblings:
        body.extend(sibling)
    for height, root in proof.other_peaks:
        body.append(height)
        body.extend(root)
    return header + bytes(body)

def parse_aggregate_evidence(evidence: bytes) -> tuple[RootStatement, AccumulatorProof]:
    if len(evidence) < AGG_EVIDENCE_HEADER.size:
        raise EvidenceError('aggregate evidence is truncated')
    magic, statement_length, prefix_count, position, path_length, other_count, direction_length = AGG_EVIDENCE_HEADER.unpack_from(evidence, 0)
    if magic != AGG_EVIDENCE_MAGIC:
        raise EvidenceError('aggregate evidence magic is invalid')
    expected_direction_length = (path_length + 7) // 8
    if direction_length != expected_direction_length:
        raise EvidenceError('direction-bit length is non-canonical')
    expected_length = AGG_EVIDENCE_HEADER.size + statement_length + direction_length + path_length * DIGEST_BYTES + other_count * (1 + DIGEST_BYTES)
    if len(evidence) != expected_length:
        raise EvidenceError('aggregate evidence length is inconsistent')
    offset = AGG_EVIDENCE_HEADER.size
    statement = RootStatement.decode(evidence[offset:offset + statement_length])
    offset += statement_length
    directions_encoded = evidence[offset:offset + direction_length]
    offset += direction_length
    siblings: list[bytes] = []
    for _ in range(path_length):
        siblings.append(evidence[offset:offset + DIGEST_BYTES])
        offset += DIGEST_BYTES
    directions = tuple((bool(directions_encoded[index // 8] & 1 << index % 8) for index in range(path_length)))
    other_peaks: list[tuple[int, bytes]] = []
    for _ in range(other_count):
        height = evidence[offset]
        offset += 1
        root = evidence[offset:offset + DIGEST_BYTES]
        offset += DIGEST_BYTES
        other_peaks.append((height, root))
    proof = AccumulatorProof(prefix_count=prefix_count, position=position, path_siblings=tuple(siblings), sibling_on_left=directions, other_peaks=tuple(other_peaks))
    return (statement, proof)

def _target_peak_index(layout: Sequence[tuple[int, int, int]], zero_based_position: int) -> int:
    for index, (_, start, end) in enumerate(layout):
        if start <= zero_based_position < end:
            return index
    raise EvidenceError('position is not covered by the certified prefix')

def verify_accumulator_proof(statement: RootStatement, proof: AccumulatorProof, expected_root: bytes, counter: HashCounter | None=None) -> bool:
    if len(expected_root) != DIGEST_BYTES:
        return False
    if statement.position != proof.position:
        return False
    if not 1 <= proof.position <= proof.prefix_count:
        return False
    layout = peak_layout(proof.prefix_count)
    try:
        target_index = _target_peak_index(layout, proof.position - 1)
    except EvidenceError:
        return False
    target_height = layout[target_index][0]
    if len(proof.path_siblings) != target_height:
        return False
    expected_other_heights = [height for index, (height, _, _) in enumerate(layout) if index != target_index]
    if [height for height, _ in proof.other_peaks] != expected_other_heights:
        return False
    node = statement_leaf_hash(statement, counter)
    for sibling, sibling_on_left in zip(proof.path_siblings, proof.sibling_on_left):
        node = acc_node_hash(sibling, node, counter) if sibling_on_left else acc_node_hash(node, sibling, counter)
    peaks: list[bytes] = []
    other_iterator = iter(proof.other_peaks)
    for index in range(len(layout)):
        if index == target_index:
            peaks.append(node)
        else:
            _, root = next(other_iterator)
            peaks.append(root)
    computed = acc_final_root(proof.prefix_count, bag_peaks(peaks, counter), counter)
    return computed == expected_root

def verify_aggregate_evidence(expected: RootStatement, evidence: bytes, anchor: AggregateAnchor, verifier: Ed25519Verifier, counter: HashCounter | None=None) -> bool:
    if expected.shard != anchor.shard:
        return False
    try:
        statement, proof = parse_aggregate_evidence(evidence)
    except (EvidenceError, ValueError, struct.error, IndexError):
        return False
    if statement != expected:
        return False
    if proof.prefix_count != anchor.prefix_count:
        return False
    if statement.position > anchor.prefix_count:
        return False
    if not verifier.verify(checkpoint_cert_message(anchor.shard, anchor.prefix_count, anchor.accumulator_root), anchor.signature):
        return False
    return verify_accumulator_proof(statement, proof, anchor.accumulator_root, counter)

def direct_evidence_bytes() -> int:
    return DIRECT_EVIDENCE_HEADER.size + ROOT_STMT_STRUCT.size + SIGNATURE_BYTES

def direct_anchor_bytes() -> int:
    return DIRECT_ANCHOR_STRUCT.size

def aggregate_anchor_bytes() -> int:
    return AGG_ANCHOR_STRUCT.size

def checkpoint_object_bytes() -> int:
    return CHECKPOINT_OBJECT_STRUCT.size

def aggregate_evidence_bytes_for_power_of_two(prefix_count: int) -> int:
    if prefix_count <= 0 or prefix_count & prefix_count - 1:
        raise ValueError('prefix_count must be a positive power of two')
    depth = int(math.log2(prefix_count))
    return AGG_EVIDENCE_HEADER.size + ROOT_STMT_STRUCT.size + (depth + 7) // 8 + depth * DIGEST_BYTES
