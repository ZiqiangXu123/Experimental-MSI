from __future__ import annotations
import hashlib
from typing import Any
from .accumulator import build_accumulator, extract_accumulator_path
from .crypto import Ed25519OpenSSL
from .encoding import aux_ref, canonical_block, encode_candidate_payload, payload_ref, root_statement
from .merkle import build_merkle, extract_path, leaf_hash
from .models import AggregateCertificate, DirectCertificate, Fixture, MSIEntry, Meta, Response
from .pipeline import checkpoint_statement
from .protocol import encode_response
from .util import next_power_of_two

def selected_positions(n: int, requested: int) -> tuple[int, ...]:
    values = {max(1, min(n, requested)), 1, n, max(1, n // 2), min(n, max(1, requested - 1)), min(n, requested + 1)}
    return tuple(sorted(values))

def _private_seed(seed: int, shard: int, epoch: int, context_bound: bool) -> bytes:
    return hashlib.sha256(b'MSI-E7-ED25519\x00' + seed.to_bytes(8, 'big', signed=False) + shard.to_bytes(4, 'big') + epoch.to_bytes(8, 'big') + bytes([1 if context_bound else 0])).digest()

def build_fixture(case: dict[str, Any], *, shard: int | None=None, epoch: int | None=None, context_bound: bool=True, malicious_target: bool=False) -> Fixture:
    n = int(case.get('epoch_length', 64))
    block_bytes = int(case.get('block_bytes', 512))
    mode = str(case.get('mode', 'full'))
    anchor_mode = str(case.get('anchor_mode', 'direct'))
    layout = str(case.get('layout', 'per_block'))
    codec = str(case.get('codec', 'raw-v1'))
    version = int(case.get('version', 1))
    seed = int(case.get('fixture_seed', 7))
    shard_value = int(case.get('shard', 11) if shard is None else shard)
    epoch_value = int(case.get('epoch', 17) if epoch is None else epoch)
    requested = int(case.get('query_position', min(n, max(1, n // 2))))
    requested = max(1, min(n, requested))
    positions = selected_positions(n, requested)
    blocks: dict[int, bytes] = {}
    leaves: list[bytes] = []
    for position in range(1, n + 1):
        block = canonical_block(shard_value, epoch_value, position, block_bytes, seed, context_payload=False)
        if malicious_target and position == requested:
            mutable = bytearray(block)
            mutable[-1] ^= 90
            block = bytes(mutable)
        blocks[position] = block
        leaves.append(leaf_hash((shard_value, epoch_value, position), block, context_bound=context_bound))
    merkle = build_merkle(leaves)
    pay_ref = payload_ref(shard_value, epoch_value, layout, codec, version)
    support_ref = aux_ref(shard_value, epoch_value, mode, merkle.root, generation=1)
    k = int(case.get('anchor_position', 7))
    meta = Meta(n=n, k=k, payload_ref=pay_ref, aux_ref=support_ref, layout=layout, mode=mode, codec=codec, version=version)
    entry = MSIEntry(root=merkle.root, meta=meta)
    openssl = Ed25519OpenSSL()
    private = _private_seed(seed, shard_value, epoch_value, context_bound)
    public = openssl.public_from_seed(private)
    statement = root_statement(shard_value, epoch_value, k, merkle.root)
    accumulator_statements: tuple[bytes, ...] = tuple()
    accumulator_levels: tuple[tuple[bytes, ...], ...] | None = None
    if anchor_mode == 'direct':
        certificate = DirectCertificate(public_key=public, statement=statement, signature=openssl.sign(private, statement))
    elif anchor_mode == 'aggregate':
        k_star = int(case.get('anchor_prefix', 64))
        if k_star < k:
            raise ValueError('anchor_prefix must cover anchor_position')
        statements: list[bytes] = []
        for position in range(1, k_star + 1):
            if position == k:
                statements.append(statement)
            else:
                dummy_root = hashlib.sha256(b'MSI-E7-DUMMY-ROOT\x00' + seed.to_bytes(8, 'big') + shard_value.to_bytes(4, 'big') + position.to_bytes(8, 'big')).digest()
                statements.append(root_statement(shard_value, 10000 + position, position, dummy_root))
        accumulator = build_accumulator(statements)
        checkpoint = checkpoint_statement(shard_value, k_star, accumulator.root)
        certificate = AggregateCertificate(public_key=public, shard=shard_value, k_star=k_star, accumulator_root=accumulator.root, checkpoint_signature=openssl.sign(private, checkpoint), statement=statement, statement_position=k, inclusion_path=extract_accumulator_path(accumulator.levels, k))
        accumulator_statements = tuple(statements)
        accumulator_levels = accumulator.levels
    else:
        raise ValueError('unsupported anchor mode')
    payloads = {position: encode_candidate_payload(blocks[position], q=(shard_value, epoch_value, position), layout=layout, codec=codec, version=version, ref=pay_ref) for position in positions}
    paths = {position: extract_path(merkle.levels, position) for position in positions}
    responses: dict[int, bytes] = {}
    for position in positions:
        witness = merkle.padded_leaves if mode == 'leaf' else paths[position]
        response = Response(q=(shard_value, epoch_value, position), root=merkle.root, meta=meta, payload=payloads[position], witness=witness, certificate=certificate)
        responses[position] = encode_response(response)
    return Fixture(shard=shard_value, epoch=epoch_value, entry=entry, payloads=payloads, paths=paths, leaf_vector=merkle.padded_leaves, certificate=certificate, private_seed=private, public_key=public, blocks=blocks, responses=responses, context_bound=context_bound, anchor_mode=anchor_mode, accumulator_statements=accumulator_statements, accumulator_levels=accumulator_levels, setup={'n': n, 'n_prime': next_power_of_two(n), 'depth': next_power_of_two(n).bit_length() - 1, 'block_bytes': block_bytes, 'mode': mode, 'layout': layout, 'codec': codec, 'version': version, 'anchor_mode': anchor_mode, 'anchor_position': k, 'root_hex': merkle.root.hex(), 'query_positions': list(positions), 'context_bound': context_bound, 'malicious_target': malicious_target, 'openssl': openssl.metadata()})

def honest_frame(fixture: Fixture, position: int) -> bytes:
    try:
        return fixture.responses[position]
    except KeyError as exc:
        raise KeyError(f'fixture has no prebuilt response for position {position}') from exc
