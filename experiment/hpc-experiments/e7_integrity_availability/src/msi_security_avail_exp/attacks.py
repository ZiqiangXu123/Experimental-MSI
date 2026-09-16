from __future__ import annotations
import struct
import zlib
from dataclasses import dataclass, replace
from typing import Any
from .constants import FIELD_ANCHOR, FIELD_META, FIELD_PAYLOAD, FIELD_QUERY, FIELD_WITNESS, FRAME_VERSION, STAGE_ANCHOR, STAGE_CHECK, STAGE_MEMBER, STAGE_RESOLVE
from .encoding import aux_ref, encode_candidate_payload, rewrite_block_context
from .fixture import build_fixture, honest_frame
from .models import AggregateCertificate, AttackMutation, CandidatePayload, DirectCertificate, Fixture, Meta, Response
from .protocol import ProtocolError, decode_anchor, decode_fields, decode_meta, decode_payload, decode_query, decode_response, decode_witness, encode_anchor, encode_fields, encode_meta, encode_payload, encode_query, encode_response, encode_witness, field_map
from .util import SplitMix64, next_power_of_two

@dataclass
class AttackEnvironment:
    case: dict[str, Any]
    target: Fixture
    other_shard: Fixture
    other_epoch: Fixture
    noctx_target: Fixture
    noctx_source: Fixture
    attacker: Fixture
    position: int

def build_attack_environment(case: dict[str, Any]) -> AttackEnvironment:
    target = build_fixture(case)
    other_shard = build_fixture(case, shard=target.shard + 101)
    other_epoch = build_fixture(case, epoch=target.epoch + 101)
    noctx_target = build_fixture(case, context_bound=False)
    noctx_source = build_fixture(case, shard=target.shard + 101, context_bound=False)
    attacker = build_fixture(case, malicious_target=True)
    position = int(case.get('query_position', min(target.entry.meta.n, max(1, target.entry.meta.n // 2))))
    position = max(1, min(target.entry.meta.n, position))
    return AttackEnvironment(case=case, target=target, other_shard=other_shard, other_epoch=other_epoch, noctx_target=noctx_target, noctx_source=noctx_source, attacker=attacker, position=position)

def _parsed_to_response(parsed: Any, **changes: Any) -> Response:
    values = {'q': parsed.q, 'root': parsed.root, 'meta': parsed.meta, 'payload': parsed.payload, 'witness': parsed.witness, 'certificate': parsed.certificate}
    values.update(changes)
    return Response(**values)

def _flip(data: bytes, offset: int, mask: int=1) -> bytes:
    if not data:
        return data
    mutable = bytearray(data)
    mutable[offset % len(mutable)] ^= mask
    return bytes(mutable)

def _payload_with_block(payload: CandidatePayload, block: bytes, *, q: tuple[int, int, int] | None=None, ref: bytes | None=None, version: int | None=None) -> CandidatePayload:
    q_value = q or (0, 0, payload.position)
    if q is None:
        q_value = (0, 0, payload.position)
    return encode_candidate_payload(block, q=q_value, layout=payload.layout, codec=payload.codec, version=payload.version if version is None else version, ref=payload.payload_ref if ref is None else ref)

def _replace_meta(frame: bytes, root: bytes, meta: Meta) -> bytes:
    fields = list(decode_fields(frame))
    return encode_fields([(fid, encode_meta(root, meta) if fid == FIELD_META else value) for fid, value in fields])

def _replace_payload(frame: bytes, payload: CandidatePayload) -> bytes:
    fields = list(decode_fields(frame))
    return encode_fields([(fid, encode_payload(payload) if fid == FIELD_PAYLOAD else value) for fid, value in fields])

def _replace_witness(frame: bytes, mode: str, aux: bytes, witness: tuple[Any, ...], n: int) -> bytes:
    fields = list(decode_fields(frame))
    encoded = encode_witness(mode, aux, witness, n)
    return encode_fields([(fid, encoded if fid == FIELD_WITNESS else value) for fid, value in fields])

def _replace_anchor(frame: bytes, anchor: DirectCertificate | AggregateCertificate) -> bytes:
    fields = list(decode_fields(frame))
    return encode_fields([(fid, encode_anchor(anchor) if fid == FIELD_ANCHOR else value) for fid, value in fields])

def _replace_query(frame: bytes, q: tuple[int, int, int]) -> bytes:
    fields = list(decode_fields(frame))
    return encode_fields([(fid, encode_query(q) if fid == FIELD_QUERY else value) for fid, value in fields])

def _operator(family: str, trial: int, fixture: Fixture) -> str:
    direct_anchor = isinstance(fixture.certificate, DirectCertificate)
    operators: dict[str, tuple[str, ...]] = {'range_padding': ('index_zero', 'index_n_plus_one', 'padded_duplicate'), 'parser_ambiguity': ('truncate', 'bad_crc', 'duplicate_field', 'unknown_field', 'field_order', 'overlong_length', 'unknown_frame_version', 'trailing_bytes'), 'metadata_locator': ('wrong_layout', 'wrong_payload_ref', 'wrong_aux_ref', 'wrong_mode', 'wrong_codec', 'wrong_version', 'payload_position', 'payload_offset'), 'payload_substitution': ('payload_bitflip', 'wrong_block', 'stale_epoch_block', 'canonical_length', 'compressed_corruption'), 'merkle_path_tampering': ('sibling_bitflip', 'path_reverse', 'wrong_direction', 'path_truncate', 'path_extend'), 'cross_context_replay': ('other_shard', 'other_epoch', 'other_position'), 'leaf_vector_tampering': ('target_leaf', 'non_target_leaf', 'vector_truncate', 'vector_extend', 'wrong_mode_vector'), 'stale_representation': ('stale_aux', 'stale_payload_ref', 'stale_version', 'stale_layout', 'stale_mode'), 'anchor_substitution': ('invalid_signature', 'wrong_statement', 'wrong_public_key') if direct_anchor else ('invalid_checkpoint_signature', 'wrong_statement', 'wrong_position', 'stale_prefix', 'forged_inclusion'), 'equivocation': ('two_payloads', 'two_paths')}
    values = operators[family]
    return values[trial % len(values)]

def _parser_mutation(frame: bytes, operator: str) -> bytes:
    fields = list(decode_fields(frame))
    if operator == 'truncate':
        return frame[:max(1, len(frame) // 2)]
    if operator == 'bad_crc':
        mutable = bytearray(frame)
        mutable[-1] ^= 128
        return bytes(mutable)
    if operator == 'duplicate_field':
        return encode_fields(fields[:2] + [fields[1]] + fields[2:])
    if operator == 'unknown_field':
        return encode_fields(fields + [(250, b'unknown')])
    if operator == 'field_order':
        return encode_fields([fields[1], fields[0]] + fields[2:])
    if operator == 'unknown_frame_version':
        return encode_fields(fields, version=FRAME_VERSION + 1)
    if operator == 'trailing_bytes':
        return encode_fields(fields, trailing=b'TRAIL')
    if operator == 'overlong_length':
        mutable = bytearray(frame)
        header_size = struct.calcsize('>4sBBHI')
        length_offset = header_size + 2
        struct.pack_into('>I', mutable, length_offset, 2147483647)
        crc = zlib.crc32(mutable[:-4]) & 4294967295
        struct.pack_into('>I', mutable, len(mutable) - 4, crc)
        return bytes(mutable)
    raise ValueError(operator)

def _replay_frame(source: Fixture, target: Fixture, position: int) -> bytes:
    source_position = min(position, source.entry.meta.n)
    parsed = decode_response(honest_frame(source, source_position))
    target_q = (target.shard, target.epoch, position)
    payload = replace(parsed.payload, payload_ref=target.entry.meta.payload_ref, position=position, logical_offset=0 if target.entry.meta.layout == 'per_block' else (position - 1) * (parsed.payload.canonical_length + 16), layout=target.entry.meta.layout, codec=target.entry.meta.codec, version=target.entry.meta.version)
    response = Response(q=target_q, root=target.entry.root, meta=target.entry.meta, payload=payload, witness=parsed.witness, certificate=target.certificate)
    return encode_response(response)

def make_attack(env: AttackEnvironment, family: str, trial: int) -> AttackMutation:
    target = env.target
    q = (target.shard, target.epoch, env.position)
    honest = honest_frame(target, env.position)
    parsed = decode_response(honest)
    operator = _operator(family, trial, target)
    expected = STAGE_CHECK
    malicious_block: bytes | None = None
    if family == 'range_padding':
        if operator == 'index_zero':
            attack_q = (q[0], q[1], 0)
        else:
            attack_q = (q[0], q[1], target.entry.meta.n + 1)
        return AttackMutation((honest,), attack_q, (STAGE_CHECK,), operator, (None,))
    if family == 'parser_ambiguity':
        return AttackMutation((_parser_mutation(honest, operator),), q, (STAGE_CHECK,), operator, (None,))
    if family == 'metadata_locator':
        if operator == 'wrong_layout':
            meta = replace(parsed.meta, layout='epoch_packed' if parsed.meta.layout == 'per_block' else 'per_block')
            frame = _replace_meta(honest, parsed.root, meta)
        elif operator == 'wrong_payload_ref':
            payload = replace(parsed.payload, payload_ref=_flip(parsed.payload.payload_ref, trial))
            frame = _replace_payload(honest, payload)
        elif operator == 'wrong_aux_ref':
            meta = replace(parsed.meta, aux_ref=_flip(parsed.meta.aux_ref, trial))
            frame = _replace_meta(honest, parsed.root, meta)
        elif operator == 'wrong_mode':
            mode = 'leaf' if parsed.meta.mode != 'leaf' else 'full'
            frame = _replace_meta(honest, parsed.root, replace(parsed.meta, mode=mode))
        elif operator == 'wrong_codec':
            meta = replace(parsed.meta, codec='zlib-v1' if parsed.meta.codec == 'raw-v1' else 'raw-v1')
            frame = _replace_meta(honest, parsed.root, meta)
        elif operator == 'wrong_version':
            frame = _replace_meta(honest, parsed.root, replace(parsed.meta, version=parsed.meta.version + 1))
        elif operator == 'payload_position':
            frame = _replace_payload(honest, replace(parsed.payload, position=max(1, q[2] - 1)))
        else:
            frame = _replace_payload(honest, replace(parsed.payload, logical_offset=parsed.payload.logical_offset + 1))
            return AttackMutation((frame,), q, (STAGE_RESOLVE,), operator, (None,))
        return AttackMutation((frame,), q, (STAGE_CHECK,), operator, (None,))
    if family == 'payload_substitution':
        if operator in {'payload_bitflip', 'wrong_block'}:
            block = bytearray(target.blocks[q[2]])
            block[-1 - trial % min(16, len(block) - 1)] ^= 64
            malicious_block = bytes(block)
            payload = encode_candidate_payload(malicious_block, q=q, layout=parsed.payload.layout, codec=parsed.payload.codec, version=parsed.payload.version, ref=parsed.payload.payload_ref)
            frame = _replace_payload(honest, payload)
            expected = STAGE_RESOLVE if parsed.meta.mode == 'leaf' else STAGE_MEMBER
        elif operator == 'stale_epoch_block':
            malicious_block = env.other_epoch.blocks[q[2]]
            payload = encode_candidate_payload(malicious_block, q=q, layout=parsed.payload.layout, codec=parsed.payload.codec, version=parsed.payload.version, ref=parsed.payload.payload_ref)
            frame = _replace_payload(honest, payload)
            expected = STAGE_RESOLVE if parsed.meta.mode == 'leaf' else STAGE_MEMBER
        elif operator == 'canonical_length':
            frame = _replace_payload(honest, replace(parsed.payload, canonical_length=parsed.payload.canonical_length + 1))
            expected = STAGE_RESOLVE
        else:
            if parsed.payload.codec == 'zlib-v1':
                encoded = _flip(parsed.payload.encoded, trial, 32)
                payload = replace(parsed.payload, encoded=encoded, encoded_crc32=zlib.crc32(encoded) & 4294967295)
            else:
                payload = replace(parsed.payload, codec='zlib-v1', encoded=b'not-zlib', encoded_crc32=zlib.crc32(b'not-zlib') & 4294967295)
                altered_meta = replace(parsed.meta, codec='zlib-v1')
                fields = list(decode_fields(honest))
                frame = encode_fields([(fid, encode_meta(parsed.root, altered_meta)) if fid == FIELD_META else (fid, encode_payload(payload)) if fid == FIELD_PAYLOAD else (fid, value) for fid, value in fields])
                return AttackMutation((frame,), q, (STAGE_CHECK,), operator, (None,))
            frame = _replace_payload(honest, payload)
            expected = STAGE_RESOLVE
        return AttackMutation((frame,), q, (expected,), operator, (malicious_block,))
    if family == 'merkle_path_tampering':
        if parsed.meta.mode == 'leaf':
            frame = _replace_witness(honest, 'full', parsed.meta.aux_ref, target.paths[q[2]], parsed.meta.n)
            return AttackMutation((frame,), q, (STAGE_CHECK,), 'leaf_mode_path_confusion', (None,))
        path = list(parsed.witness)
        if operator == 'sibling_bitflip':
            digest, direction = path[trial % len(path)]
            path[trial % len(path)] = (_flip(digest, trial), direction)
            expected = STAGE_MEMBER
        elif operator == 'path_reverse':
            path.reverse()
            expected = STAGE_MEMBER
        elif operator == 'wrong_direction':
            index = trial % len(path)
            digest, direction = path[index]
            path[index] = (digest, 1 - direction)
            expected = STAGE_MEMBER
        elif operator == 'path_truncate':
            path = path[:-1]
            expected = STAGE_CHECK
        else:
            path.append(path[-1] if path else (b'\x00' * 32, 0))
            expected = STAGE_CHECK
        frame = _replace_witness(honest, parsed.meta.mode, parsed.meta.aux_ref, tuple(path), parsed.meta.n)
        return AttackMutation((frame,), q, (expected,), operator, (None,))
    if family == 'cross_context_replay':
        if operator == 'other_shard':
            frame = _replay_frame(env.other_shard, target, q[2])
            malicious_block = env.other_shard.blocks[q[2]]
        elif operator == 'other_epoch':
            frame = _replay_frame(env.other_epoch, target, q[2])
            malicious_block = env.other_epoch.blocks[q[2]]
        else:
            other_position = 1 if q[2] != 1 else min(target.entry.meta.n, 2)
            source = decode_response(honest_frame(target, other_position))
            payload = replace(source.payload, position=q[2], logical_offset=0 if parsed.meta.layout == 'per_block' else (q[2] - 1) * (source.payload.canonical_length + 16))
            frame = encode_response(Response(q=q, root=parsed.root, meta=parsed.meta, payload=payload, witness=source.witness, certificate=parsed.certificate))
            malicious_block = target.blocks[other_position]
        replay_stage = STAGE_RESOLVE if parsed.meta.mode == 'leaf' else STAGE_MEMBER
        return AttackMutation((frame,), q, (replay_stage,), operator, (malicious_block,))
    if family == 'leaf_vector_tampering':
        if parsed.meta.mode != 'leaf':
            frame = _replace_witness(honest, 'leaf', parsed.meta.aux_ref, target.leaf_vector, parsed.meta.n)
            return AttackMutation((frame,), q, (STAGE_CHECK,), 'leaf_object_in_nonleaf_mode', (None,))
        vector = list(parsed.witness)
        if operator == 'target_leaf':
            vector[q[2] - 1] = _flip(vector[q[2] - 1], trial)
            expected = STAGE_RESOLVE
        elif operator == 'non_target_leaf':
            index = 0 if q[2] != 1 else min(1, len(vector) - 1)
            vector[index] = _flip(vector[index], trial)
            expected = STAGE_MEMBER
        elif operator == 'vector_truncate':
            vector = vector[:-1]
            expected = STAGE_CHECK
        elif operator == 'vector_extend':
            vector.append(vector[-1])
            expected = STAGE_CHECK
        else:
            frame = _replace_witness(honest, 'full', parsed.meta.aux_ref, target.paths[q[2]], parsed.meta.n)
            return AttackMutation((frame,), q, (STAGE_CHECK,), operator, (None,))
        frame = _replace_witness(honest, 'leaf', parsed.meta.aux_ref, tuple(vector), parsed.meta.n)
        return AttackMutation((frame,), q, (expected,), operator, (None,))
    if family == 'stale_representation':
        if operator == 'stale_aux':
            stale = aux_ref(target.shard, target.epoch, parsed.meta.mode, parsed.root, generation=0)
            frame = _replace_meta(honest, parsed.root, replace(parsed.meta, aux_ref=stale))
        elif operator == 'stale_payload_ref':
            frame = _replace_payload(honest, replace(parsed.payload, payload_ref=_flip(parsed.payload.payload_ref, 0)))
        elif operator == 'stale_version':
            meta = replace(parsed.meta, version=parsed.meta.version + 1)
            payload = replace(parsed.payload, version=parsed.payload.version + 1)
            fields = list(decode_fields(honest))
            frame = encode_fields([(fid, encode_meta(parsed.root, meta)) if fid == FIELD_META else (fid, encode_payload(payload)) if fid == FIELD_PAYLOAD else (fid, value) for fid, value in fields])
        elif operator == 'stale_layout':
            frame = _replace_meta(honest, parsed.root, replace(parsed.meta, layout='epoch_packed' if parsed.meta.layout == 'per_block' else 'per_block'))
        else:
            frame = _replace_meta(honest, parsed.root, replace(parsed.meta, mode='ext' if parsed.meta.mode != 'ext' else 'full'))
        return AttackMutation((frame,), q, (STAGE_CHECK,), operator, (None,))
    if family == 'anchor_substitution':
        cert = parsed.certificate
        if isinstance(cert, DirectCertificate):
            if operator == 'invalid_signature':
                changed = replace(cert, signature=_flip(cert.signature, trial))
            elif operator == 'wrong_public_key':
                changed = replace(cert, public_key=_flip(cert.public_key, trial))
            else:
                changed = replace(cert, statement=_flip(cert.statement, len(cert.statement) - 1))
        elif operator == 'invalid_checkpoint_signature':
            changed = replace(cert, checkpoint_signature=_flip(cert.checkpoint_signature, trial))
        elif operator == 'wrong_statement':
            changed = replace(cert, statement=_flip(cert.statement, len(cert.statement) - 1))
        elif operator == 'wrong_position':
            changed = replace(cert, statement_position=cert.statement_position + 1)
        elif operator == 'stale_prefix':
            changed = replace(cert, k_star=max(1, cert.statement_position - 1))
        else:
            path = list(cert.inclusion_path)
            digest, direction = path[0]
            path[0] = (_flip(digest, trial), direction)
            changed = replace(cert, inclusion_path=tuple(path))
        frame = _replace_anchor(honest, changed)
        return AttackMutation((frame,), q, (STAGE_ANCHOR,), operator, (None,))
    if family == 'equivocation':
        if operator == 'two_payloads':
            frames = []
            blocks = []
            for mask in (32, 64):
                block = bytearray(target.blocks[q[2]])
                block[-1] ^= mask
                bad = bytes(block)
                payload = encode_candidate_payload(bad, q=q, layout=parsed.payload.layout, codec=parsed.payload.codec, version=parsed.payload.version, ref=parsed.payload.payload_ref)
                frames.append(_replace_payload(honest, payload))
                blocks.append(bad)
            payload_stage = STAGE_RESOLVE if parsed.meta.mode == 'leaf' else STAGE_MEMBER
            return AttackMutation(tuple(frames), q, (payload_stage, payload_stage), operator, tuple(blocks))
        if parsed.meta.mode == 'leaf':
            vector1 = list(parsed.witness)
            vector2 = list(parsed.witness)
            idx = 0 if q[2] != 1 else min(1, len(vector1) - 1)
            vector1[idx] = _flip(vector1[idx], 0, 1)
            vector2[idx] = _flip(vector2[idx], 0, 2)
            return AttackMutation((_replace_witness(honest, 'leaf', parsed.meta.aux_ref, tuple(vector1), parsed.meta.n), _replace_witness(honest, 'leaf', parsed.meta.aux_ref, tuple(vector2), parsed.meta.n)), q, (STAGE_MEMBER, STAGE_MEMBER), operator, (None, None))
        path1 = list(parsed.witness)
        path2 = list(parsed.witness)
        d1, direction1 = path1[0]
        d2, direction2 = path2[0]
        path1[0] = (_flip(d1, 0, 1), direction1)
        path2[0] = (_flip(d2, 0, 2), direction2)
        return AttackMutation((_replace_witness(honest, parsed.meta.mode, parsed.meta.aux_ref, tuple(path1), parsed.meta.n), _replace_witness(honest, parsed.meta.mode, parsed.meta.aux_ref, tuple(path2), parsed.meta.n)), q, (STAGE_MEMBER, STAGE_MEMBER), operator, (None, None))
    raise ValueError(f'unsupported attack family {family}')

def make_ablation_attack(env: AttackEnvironment, ablation: str) -> AttackMutation:
    target = env.target
    q = (target.shard, target.epoch, env.position)
    if ablation == 'no_ctx':
        frame = _replay_frame(env.noctx_source, env.noctx_target, env.position)
        return AttackMutation((frame,), (env.noctx_target.shard, env.noctx_target.epoch, env.position), (None,), 'cross_context_replay_no_ctx', (env.noctx_source.blocks[env.position],), unsafe_fixture=env.noctx_target, unsafe_ablation='no_ctx')
    if ablation == 'no_anchor':
        attack_parsed = decode_response(honest_frame(env.attacker, env.position))
        safe_meta = target.entry.meta
        response = Response(q=q, root=attack_parsed.root, meta=safe_meta, payload=replace(attack_parsed.payload, payload_ref=safe_meta.payload_ref), witness=attack_parsed.witness, certificate=attack_parsed.certificate)
        frame = encode_response(response)
        return AttackMutation((frame,), q, (None,), 'attacker_chosen_root_no_anchor', (env.attacker.blocks[env.position],), unsafe_fixture=target, unsafe_ablation='no_anchor')
    if ablation == 'no_meta':
        parsed = decode_response(honest_frame(target, env.position))
        stale_meta = replace(parsed.meta, aux_ref=_flip(parsed.meta.aux_ref, 0), version=99)
        stale_payload = replace(parsed.payload, version=99)
        response = Response(q=q, root=parsed.root, meta=stale_meta, payload=stale_payload, witness=parsed.witness, certificate=parsed.certificate)
        return AttackMutation((encode_response(response),), q, (None,), 'version_and_locator_confusion_no_meta', (target.blocks[env.position],), unsafe_fixture=target, unsafe_ablation='no_meta', semantic_attack=True)
    raise ValueError(ablation)
