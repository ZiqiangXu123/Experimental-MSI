from __future__ import annotations
import time
from typing import Callable
from .accumulator import verify_accumulator
from .constants import DIGEST_BYTES, STAGE_ACCEPT, STAGE_ANCHOR, STAGE_CHECK, STAGE_CRASH, STAGE_MEMBER, STAGE_RESOLVE
from .crypto import Ed25519OpenSSL, OperationCounter
from .encoding import decode_candidate_payload, root_statement
from .merkle import leaf_hash, rebuild_path, verify_member
from .models import AggregateCertificate, Decision, DirectCertificate, Fixture, Meta
from .protocol import ProtocolError, decode_response
from .util import next_power_of_two

def checkpoint_statement(shard: int, k_star: int, accumulator_root: bytes) -> bytes:
    from .constants import CHECKPOINT_TAG
    import struct
    return CHECKPOINT_TAG + struct.pack('>IQ', shard, k_star) + accumulator_root

def _meta_equal(left: Meta, right: Meta) -> bool:
    return left.n == right.n and left.k == right.k and (left.payload_ref == right.payload_ref) and (left.aux_ref == right.aux_ref) and (left.layout == right.layout) and (left.mode == right.mode) and (left.codec == right.codec) and (left.version == right.version)

def verify_frame(frame: bytes, query: tuple[int, int, int], fixture: Fixture, *, ablation: str='safe', openssl: Ed25519OpenSSL | None=None, coverage_sink: Callable[[str], None] | None=None) -> Decision:
    start = time.perf_counter_ns()
    phase_ns = {STAGE_CHECK: 0, STAGE_RESOLVE: 0, STAGE_MEMBER: 0, STAGE_ANCHOR: 0}
    counter = OperationCounter()
    guards: list[str] = []

    def mark(guard: str) -> None:
        guards.append(guard)
        if coverage_sink is not None:
            coverage_sink(guard)

    def reject(stage: str, reason: str, *, block: bytes | None=None, root: bytes | None=None, metadata_violation: bool=False) -> Decision:
        return Decision(accepted=False, stage=stage, reason=reason, latency_ns=time.perf_counter_ns() - start, phase_ns=dict(phase_ns), hash_calls=counter.hash_calls, signature_verifications=counter.signature_verifications, guards=tuple(dict.fromkeys(guards)), block=block, root_used=root, metadata_violation=metadata_violation)
    try:
        t0 = time.perf_counter_ns()
        try:
            parsed = decode_response(frame, mark)
        except ProtocolError as exc:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, exc.reason)
        mark('range.valid')
        if not 1 <= query[2] <= fixture.entry.meta.n:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'query_out_of_range')
        mark('query.context')
        if parsed.q != query:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'response_query_mismatch')
        mark('meta.root_width')
        if len(parsed.root) != DIGEST_BYTES:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'root_width')
        meta_fields_violation = not _meta_equal(parsed.meta, fixture.entry.meta)
        root_violation = parsed.root != fixture.entry.root
        metadata_violation = root_violation or meta_fields_violation
        mark('meta.equal')
        if ablation == 'no_anchor':
            if meta_fields_violation:
                phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
                return reject(STAGE_CHECK, 'metadata_mismatch', metadata_violation=True)
        elif ablation != 'no_meta' and metadata_violation:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'metadata_mismatch', metadata_violation=True)
        active_meta = parsed.meta if ablation == 'no_meta' else fixture.entry.meta
        mark('meta.version_registry')
        if ablation != 'no_meta' and (not (active_meta.version == 1 and active_meta.codec in {'raw-v1', 'zlib-v1'} and (active_meta.layout in {'per_block', 'epoch_packed'}) and (active_meta.mode in {'full', 'leaf', 'ext'}))):
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'version_registry')
        mark('payload.ref')
        if ablation != 'no_meta' and parsed.payload.payload_ref != active_meta.payload_ref:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'payload_ref')
        mark('payload.meta')
        if ablation != 'no_meta' and (parsed.payload.layout != active_meta.layout or parsed.payload.codec != active_meta.codec or parsed.payload.version != active_meta.version or (parsed.payload.position != query[2])):
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'payload_metadata')
        mark('payload.envelope')
        if parsed.payload.canonical_length < 36 or parsed.payload.position < 1:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'payload_envelope')
        mark('witness.mode')
        if ablation != 'no_meta' and parsed.meta.mode != active_meta.mode:
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'witness_mode')
        n_prime = next_power_of_two(active_meta.n)
        depth = n_prime.bit_length() - 1
        mark('witness.shape')
        if active_meta.mode == 'leaf':
            if len(parsed.witness) != n_prime or any((not isinstance(item, bytes) or len(item) != 32 for item in parsed.witness)):
                phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
                return reject(STAGE_CHECK, 'leaf_vector_shape')
        else:
            if len(parsed.witness) != depth:
                phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
                return reject(STAGE_CHECK, 'path_shape')
            for item in parsed.witness:
                if not isinstance(item, tuple) or len(item) != 2 or len(item[0]) != 32 or (item[1] not in (0, 1)):
                    phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
                    return reject(STAGE_CHECK, 'path_item_shape')
        mark('anchor.shape')
        if not isinstance(parsed.certificate, (DirectCertificate, AggregateCertificate)):
            phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
            return reject(STAGE_CHECK, 'anchor_shape')
        phase_ns[STAGE_CHECK] = time.perf_counter_ns() - t0
        t0 = time.perf_counter_ns()
        counter.set_phase(STAGE_RESOLVE)
        mark('payload.decompress')
        block = decode_candidate_payload(parsed.payload, query, enforce_context=False)
        mark('payload.canonical')
        if block is None:
            phase_ns[STAGE_RESOLVE] = time.perf_counter_ns() - t0
            return reject(STAGE_RESOLVE, 'payload_decode', metadata_violation=metadata_violation)
        mode = active_meta.mode
        context_bound = ablation != 'no_ctx'
        if mode == 'leaf':
            vector = parsed.witness
            if not isinstance(vector, tuple):
                phase_ns[STAGE_RESOLVE] = time.perf_counter_ns() - t0
                return reject(STAGE_RESOLVE, 'leaf_vector_type', block=block, metadata_violation=metadata_violation)
            mark('leaf.target_entry')
            if leaf_hash(query, block, counter, context_bound=context_bound) != vector[query[2] - 1]:
                phase_ns[STAGE_RESOLVE] = time.perf_counter_ns() - t0
                return reject(STAGE_RESOLVE, 'leaf_target_mismatch', block=block, metadata_violation=metadata_violation)
            mark('leaf.rebuild')
            path = rebuild_path(query[2], vector, counter)
            if path is None:
                phase_ns[STAGE_RESOLVE] = time.perf_counter_ns() - t0
                return reject(STAGE_RESOLVE, 'leaf_rebuild', block=block, metadata_violation=metadata_violation)
        else:
            path = parsed.witness
        phase_ns[STAGE_RESOLVE] = time.perf_counter_ns() - t0
        t0 = time.perf_counter_ns()
        counter.set_phase(STAGE_MEMBER)
        root_used = parsed.root if ablation == 'no_anchor' else fixture.entry.root
        mark('member.depth')
        mark('member.digest_width')
        mark('member.direction')
        member_ok, member_reason = verify_member(query, block, path, root_used, active_meta.n, counter, context_bound=context_bound)
        mark('member.root')
        phase_ns[STAGE_MEMBER] = time.perf_counter_ns() - t0
        if not member_ok:
            return reject(STAGE_MEMBER, member_reason, block=block, root=root_used, metadata_violation=metadata_violation)
        t0 = time.perf_counter_ns()
        counter.set_phase(STAGE_ANCHOR)
        if ablation == 'no_anchor':
            anchor_ok = True
        else:
            backend = openssl or Ed25519OpenSSL()
            certificate = parsed.certificate
            expected_k = fixture.entry.meta.k
            expected_statement = root_statement(query[0], query[1], expected_k, fixture.entry.root)
            mark('anchor.statement')
            if isinstance(certificate, DirectCertificate):
                statement_ok = certificate.statement == expected_statement
                mark('anchor.public_key')
                key_ok = certificate.public_key == fixture.public_key
                mark('anchor.signature')
                anchor_ok = statement_ok and key_ok and backend.verify(certificate.public_key, certificate.statement, certificate.signature, counter)
            else:
                mark('anchor.prefix')
                prefix_ok = certificate.shard == query[0] and expected_k <= certificate.k_star and (certificate.statement_position == expected_k) and (certificate.statement == expected_statement)
                mark('anchor.public_key')
                key_ok = certificate.public_key == fixture.public_key
                mark('anchor.signature')
                checkpoint_ok = key_ok and backend.verify(certificate.public_key, checkpoint_statement(certificate.shard, certificate.k_star, certificate.accumulator_root), certificate.checkpoint_signature, counter)
                mark('anchor.inclusion')
                inclusion_ok = verify_accumulator(certificate.statement, certificate.statement_position, certificate.k_star, certificate.inclusion_path, certificate.accumulator_root, counter)
                anchor_ok = prefix_ok and checkpoint_ok and inclusion_ok
        phase_ns[STAGE_ANCHOR] = time.perf_counter_ns() - t0
        if not anchor_ok:
            return reject(STAGE_ANCHOR, 'anchor_invalid', block=block, root=root_used, metadata_violation=metadata_violation)
        mark('accept.complete')
        return Decision(accepted=True, stage=STAGE_ACCEPT, reason='accepted', latency_ns=time.perf_counter_ns() - start, phase_ns=dict(phase_ns), hash_calls=counter.hash_calls, signature_verifications=counter.signature_verifications, guards=tuple(dict.fromkeys(guards)), block=block, root_used=root_used, metadata_violation=metadata_violation)
    except BaseException as exc:
        return Decision(accepted=False, stage=STAGE_CRASH, reason=f'{type(exc).__name__}: {exc}', latency_ns=time.perf_counter_ns() - start, phase_ns=dict(phase_ns), hash_calls=counter.hash_calls, signature_verifications=counter.signature_verifications, guards=tuple(dict.fromkeys(guards)), exception=f'{type(exc).__name__}: {exc}')
