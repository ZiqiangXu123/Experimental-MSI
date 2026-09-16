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
    encoded: bytes
    canonical_length: int
    logical_offset: int
    encoded_crc32: int

@dataclass(frozen=True)
class DirectCertificate:
    public_key: bytes
    statement: bytes
    signature: bytes

@dataclass(frozen=True)
class Response:
    q: Query
    payload: CandidatePayload
    aux_ref: bytes
    mode: str
    witness: MerklePath | tuple[bytes, ...]
    certificate: DirectCertificate

@dataclass
class ServerFixture:
    shard: int
    epoch: int
    target_position_pool: tuple[int, ...]
    entry: MSIEntry
    responses: dict[int, Response]
    setup_metadata: dict[str, Any]

@dataclass
class VerifierContext:
    shard: int
    epoch: int
    target_position_pool: tuple[int, ...]
    entry: MSIEntry
    anchor_backend: Any
    anchor_cached_valid: bool
    setup_metadata: dict[str, Any]
