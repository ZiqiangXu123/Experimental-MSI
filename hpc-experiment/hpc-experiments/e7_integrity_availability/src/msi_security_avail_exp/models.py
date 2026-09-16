from __future__ import annotations
from dataclasses import dataclass
from typing import Any
Query = tuple[int, int, int]
MerklePath = tuple[tuple[bytes, int], ...]

@dataclass(frozen=True)
class Meta:
    n: int
    k: int
    payload_ref: bytes
    aux_ref: bytes
    layout: str
    mode: str
    codec: str
    version: int

@dataclass(frozen=True)
class MSIEntry:
    root: bytes
    meta: Meta

@dataclass(frozen=True)
class CandidatePayload:
    payload_ref: bytes
    layout: str
    codec: str
    version: int
    position: int
    canonical_length: int
    logical_offset: int
    encoded_crc32: int
    encoded: bytes

@dataclass(frozen=True)
class DirectCertificate:
    public_key: bytes
    statement: bytes
    signature: bytes

@dataclass(frozen=True)
class AggregateCertificate:
    public_key: bytes
    shard: int
    k_star: int
    accumulator_root: bytes
    checkpoint_signature: bytes
    statement: bytes
    statement_position: int
    inclusion_path: MerklePath
AnchorEvidence = DirectCertificate | AggregateCertificate

@dataclass(frozen=True)
class Response:
    q: Query
    root: bytes
    meta: Meta
    payload: CandidatePayload
    witness: MerklePath | tuple[bytes, ...]
    certificate: AnchorEvidence

@dataclass
class Fixture:
    shard: int
    epoch: int
    entry: MSIEntry
    payloads: dict[int, CandidatePayload]
    paths: dict[int, MerklePath]
    leaf_vector: tuple[bytes, ...]
    certificate: AnchorEvidence
    private_seed: bytes
    public_key: bytes
    blocks: dict[int, bytes]
    responses: dict[int, bytes]
    context_bound: bool
    anchor_mode: str
    accumulator_statements: tuple[bytes, ...]
    accumulator_levels: tuple[tuple[bytes, ...], ...] | None
    setup: dict[str, Any]

@dataclass(frozen=True)
class ParsedFrame:
    q: Query
    root: bytes
    meta: Meta
    payload: CandidatePayload
    witness: MerklePath | tuple[bytes, ...]
    certificate: AnchorEvidence
    raw_fields: tuple[tuple[int, bytes], ...]

@dataclass(frozen=True)
class Decision:
    accepted: bool
    stage: str
    reason: str
    latency_ns: int
    phase_ns: dict[str, int]
    hash_calls: int
    signature_verifications: int
    guards: tuple[str, ...]
    block: bytes | None = None
    root_used: bytes | None = None
    metadata_violation: bool = False
    exception: str | None = None

@dataclass(frozen=True)
class AttackMutation:
    frames: tuple[bytes, ...]
    query: Query
    expected_stages: tuple[str | None, ...]
    mutation_name: str
    malicious_blocks: tuple[bytes | None, ...]
    unsafe_fixture: Fixture | None = None
    unsafe_ablation: str | None = None
    semantic_attack: bool = True
