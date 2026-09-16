from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any
from .constants import DIGEST_BYTES, MODE_BY_SCHEME
from .crypto import OperationCounter
from .encoding import check_payload_envelope, decode_candidate_payload, root_statement
from .merkle import leaf_hash, rebuild_path, verify_member
from .models import Fixture, MSIEntry, MerklePath, Query, Response
from .util import is_power_of_two, next_power_of_two

@dataclass(frozen=True)
class PreparedQuery:
    q: Query
    entry: MSIEntry | None
    response: Response | None
    raw_block: bytes | None

def prepare_query(fixture: Fixture, position: int, case: dict[str, Any]) -> PreparedQuery | None:
    scheme = str(case['scheme'])
    q = (fixture.shard, fixture.epoch, position)
    if scheme == 'B0_raw':
        if fixture.local_blocks is None:
            return None
        block = fixture.local_blocks.get(position)
        if block is None:
            return None
        return PreparedQuery(q=q, entry=None, response=None, raw_block=block)
    entry = fixture.index.get(fixture.epoch)
    if entry is None:
        return None
    response = fixture.responses.get(position)
    if response is None:
        return None
    return PreparedQuery(q=q, entry=entry, response=response, raw_block=None)

def _check_response(prepared: PreparedQuery, case: dict[str, Any], fixture: Fixture) -> bool:
    if prepared.entry is None or prepared.response is None:
        return False
    q = prepared.q
    entry = prepared.entry
    response = prepared.response
    meta = entry.meta
    mode = MODE_BY_SCHEME[str(case['scheme'])]
    if len(entry.root) != DIGEST_BYTES:
        return False
    if len(meta.payload_ref) != DIGEST_BYTES or len(meta.aux_ref) != DIGEST_BYTES:
        return False
    if meta.n < 1 or meta.mode != mode:
        return False
    if response.q != q:
        return False
    if q[2] < 1 or q[2] > meta.n:
        return False
    payload = response.payload
    if payload.payload_ref != meta.payload_ref:
        return False
    if payload.layout != meta.layout or payload.codec != meta.codec or payload.version != meta.version:
        return False
    if response.aux_ref != meta.aux_ref or response.mode != meta.mode:
        return False
    if not check_payload_envelope(payload, q):
        return False
    depth = next_power_of_two(meta.n).bit_length() - 1
    if mode in {'full', 'ext'}:
        path = response.witness
        if not isinstance(path, tuple) or len(path) != depth:
            return False
        for item in path:
            if not isinstance(item, tuple) or len(item) != 2:
                return False
            digest, direction = item
            if not isinstance(digest, bytes) or len(digest) != DIGEST_BYTES:
                return False
            if direction not in (0, 1):
                return False
    elif mode == 'leaf':
        vector = response.witness
        if not isinstance(vector, tuple):
            return False
        if len(vector) != next_power_of_two(meta.n) or not is_power_of_two(len(vector)):
            return False
        if any((not isinstance(d, bytes) or len(d) != DIGEST_BYTES for d in vector)):
            return False
    else:
        return False
    cert = response.certificate
    if len(cert.public_key) != 32 or len(cert.signature) != 64:
        return False
    if cert.public_key != fixture.anchor_backend.public_key:
        return False
    return True

def accept_prepared(prepared: PreparedQuery, fixture: Fixture, case: dict[str, Any], counter: OperationCounter | None=None) -> bytes | None:
    scheme = str(case['scheme'])
    ablation = str(case.get('ablation', 'safe'))
    if scheme == 'B0_raw':
        return prepared.raw_block
    if prepared.entry is None or prepared.response is None:
        return None
    q = prepared.q
    entry = prepared.entry
    response = prepared.response
    if counter is not None:
        counter.set_phase('check_resp')
    if ablation != 'no_checkresp' and (not _check_response(prepared, case, fixture)):
        return None
    if counter is not None:
        counter.set_phase('decode_payload')
    block = decode_candidate_payload(response.payload, q, counter)
    if block is None:
        return None
    if counter is not None:
        counter.set_phase('resolve_witness')
    mode = MODE_BY_SCHEME[scheme]
    if mode == 'leaf':
        vector = response.witness
        if not isinstance(vector, tuple):
            return None
        if leaf_hash(q, block, counter) != vector[q[2] - 1]:
            return None
        path = rebuild_path(q[2], vector, counter)
        if path is None:
            return None
    else:
        witness = response.witness
        if not isinstance(witness, tuple):
            return None
        path = witness
    if counter is not None:
        counter.set_phase('verify_member')
    if not verify_member(q, block, path, entry.root, entry.meta.n, counter):
        return None
    if counter is not None:
        counter.set_phase('verify_anchor')
    if ablation == 'cached_anchor':
        anchor_ok = fixture.anchor_cached_valid
    else:
        cert = response.certificate
        expected = root_statement(q[0], q[1], entry.meta.k, entry.root)
        anchor_ok = cert.statement == expected and fixture.anchor_backend.verify(cert.statement, cert.signature, counter)
    if not anchor_ok:
        return None
    return block

def execute_instrumented(fixture: Fixture, position: int, case: dict[str, Any]) -> tuple[bool, dict[str, int]]:
    scheme = str(case['scheme'])
    phases: dict[str, int] = {'lookup_msi': 0, 'handoff': 0, 'check_resp': 0, 'decode_payload': 0, 'resolve_witness': 0, 'verify_member': 0, 'verify_anchor': 0}
    q = (fixture.shard, fixture.epoch, position)
    if scheme == 'B0_raw':
        t0 = time.perf_counter_ns()
        block = fixture.local_blocks.get(position) if fixture.local_blocks is not None else None
        phases['lookup_msi'] = time.perf_counter_ns() - t0
        return (block is not None, phases)
    t0 = time.perf_counter_ns()
    entry = fixture.index.get(fixture.epoch)
    phases['lookup_msi'] = time.perf_counter_ns() - t0
    if entry is None:
        return (False, phases)
    t0 = time.perf_counter_ns()
    response = fixture.responses.get(position)
    phases['handoff'] = time.perf_counter_ns() - t0
    if response is None:
        return (False, phases)
    prepared = PreparedQuery(q=q, entry=entry, response=response, raw_block=None)
    ablation = str(case.get('ablation', 'safe'))
    t0 = time.perf_counter_ns()
    check_ok = True if ablation == 'no_checkresp' else _check_response(prepared, case, fixture)
    phases['check_resp'] = time.perf_counter_ns() - t0
    if not check_ok:
        return (False, phases)
    t0 = time.perf_counter_ns()
    block = decode_candidate_payload(response.payload, q)
    phases['decode_payload'] = time.perf_counter_ns() - t0
    if block is None:
        return (False, phases)
    mode = MODE_BY_SCHEME[scheme]
    t0 = time.perf_counter_ns()
    if mode == 'leaf':
        vector = response.witness
        if not isinstance(vector, tuple) or leaf_hash(q, block) != vector[q[2] - 1]:
            path = None
        else:
            path = rebuild_path(q[2], vector)
    else:
        path = response.witness if isinstance(response.witness, tuple) else None
    phases['resolve_witness'] = time.perf_counter_ns() - t0
    if path is None:
        return (False, phases)
    t0 = time.perf_counter_ns()
    member_ok = verify_member(q, block, path, entry.root, entry.meta.n)
    phases['verify_member'] = time.perf_counter_ns() - t0
    if not member_ok:
        return (False, phases)
    t0 = time.perf_counter_ns()
    if ablation == 'cached_anchor':
        anchor_ok = fixture.anchor_cached_valid
    else:
        cert = response.certificate
        expected = root_statement(q[0], q[1], entry.meta.k, entry.root)
        anchor_ok = cert.statement == expected and fixture.anchor_backend.verify(cert.statement, cert.signature)
    phases['verify_anchor'] = time.perf_counter_ns() - t0
    return (bool(anchor_ok), phases)
