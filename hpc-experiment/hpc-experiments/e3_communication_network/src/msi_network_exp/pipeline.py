from __future__ import annotations
import time
from typing import Any
from .constants import DIGEST_BYTES, MODE_BY_SCHEME
from .crypto import OperationCounter
from .encoding import check_payload_envelope, decode_candidate_payload, root_statement
from .merkle import leaf_hash, rebuild_path, verify_member
from .models import Response, VerifierContext
from .util import is_power_of_two, next_power_of_two
PHASES = ('check_resp', 'decode_payload', 'resolve_witness', 'verify_member', 'verify_anchor')

def check_response(context: VerifierContext, response: Response, case: dict[str, Any]) -> bool:
    q = response.q
    entry = context.entry
    meta = entry.meta
    mode = MODE_BY_SCHEME[str(case['scheme'])]
    if len(entry.root) != DIGEST_BYTES:
        return False
    if len(meta.payload_ref) != DIGEST_BYTES or len(meta.aux_ref) != DIGEST_BYTES:
        return False
    if meta.n < 1 or meta.mode != mode:
        return False
    if q[0] != context.shard or q[1] != context.epoch:
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
    if cert.public_key != context.anchor_backend.public_key:
        return False
    return True

def accept_response(context: VerifierContext, response: Response, case: dict[str, Any], counter: OperationCounter | None=None) -> bytes | None:
    q = response.q
    entry = context.entry
    if counter is not None:
        counter.set_phase('check_resp')
    if not check_response(context, response, case):
        return None
    if counter is not None:
        counter.set_phase('decode_payload')
    block = decode_candidate_payload(response.payload, q, counter)
    if block is None:
        return None
    if counter is not None:
        counter.set_phase('resolve_witness')
    mode = MODE_BY_SCHEME[str(case['scheme'])]
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
    cert = response.certificate
    expected = root_statement(q[0], q[1], entry.meta.k, entry.root)
    if cert.statement != expected:
        return None
    if not context.anchor_backend.verify(cert.statement, cert.signature, counter):
        return None
    return block

def accept_response_instrumented(context: VerifierContext, response: Response, case: dict[str, Any]) -> tuple[bool, dict[str, int]]:
    phases = {name: 0 for name in PHASES}
    q = response.q
    entry = context.entry
    t0 = time.perf_counter_ns()
    ok = check_response(context, response, case)
    phases['check_resp'] = time.perf_counter_ns() - t0
    if not ok:
        return (False, phases)
    t0 = time.perf_counter_ns()
    block = decode_candidate_payload(response.payload, q)
    phases['decode_payload'] = time.perf_counter_ns() - t0
    if block is None:
        return (False, phases)
    t0 = time.perf_counter_ns()
    mode = MODE_BY_SCHEME[str(case['scheme'])]
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
    cert = response.certificate
    expected = root_statement(q[0], q[1], entry.meta.k, entry.root)
    anchor_ok = cert.statement == expected and context.anchor_backend.verify(cert.statement, cert.signature)
    phases['verify_anchor'] = time.perf_counter_ns() - t0
    return (bool(anchor_ok), phases)
