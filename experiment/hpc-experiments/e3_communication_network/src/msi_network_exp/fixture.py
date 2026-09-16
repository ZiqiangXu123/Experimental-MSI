from __future__ import annotations
import gc
import hashlib
import random
import time
from typing import Any
from .constants import MODE_BY_SCHEME
from .crypto import Ed25519OpenSSL, Ed25519Verifier
from .encoding import aux_ref, canonical_block, encode_candidate_payload, payload_ref, root_statement
from .merkle import build_merkle, extract_path, leaf_hash
from .models import DirectCertificate, MSIEntry, Meta, Response, ServerFixture, VerifierContext
from .util import get_rss_kib, next_power_of_two, stable_seed

def query_position_pool(n: int, pool_size: int, seed: int) -> tuple[int, ...]:
    if n < 1:
        raise ValueError('n must be positive')
    if pool_size < 1:
        raise ValueError('query_pool_size must be positive')
    if n <= pool_size:
        return tuple(range(1, n + 1))
    positions = {1, 2, n, max(1, n - 1), max(1, n // 2), min(n, n // 2 + 1), max(1, n // 4), min(n, 3 * n // 4)}
    rng = random.Random(seed)
    while len(positions) < min(pool_size, n):
        positions.add(rng.randint(1, n))
    return tuple(sorted(positions))

def _build_common(case: dict[str, Any], retain_responses: bool) -> tuple[int, int, tuple[int, ...], MSIEntry, dict[int, Response], dict[str, Any], bytes]:
    start = time.perf_counter_ns()
    rss_before = get_rss_kib()
    scheme = str(case['scheme'])
    mode = MODE_BY_SCHEME[scheme]
    n = int(case['epoch_length'])
    block_bytes = int(case['block_bytes'])
    layout = str(case.get('layout', 'epoch_packed'))
    codec = str(case.get('codec', 'raw-v1'))
    version = int(case.get('version', 1))
    seed = int(case['fixture_seed'])
    shard = int(case.get('shard', 1))
    epoch = int(case.get('historical_epochs', 1000))
    pool = query_position_pool(n, int(case['query_pool_size']), int(case['query_seed']))
    selected = set(pool)
    leaves: list[bytes] = []
    selected_blocks: dict[int, bytes] = {}
    for position in range(1, n + 1):
        block = canonical_block(shard, epoch, position, block_bytes, seed)
        leaves.append(leaf_hash((shard, epoch, position), block))
        if retain_responses and position in selected:
            selected_blocks[position] = block
    merkle = build_merkle(leaves)
    root = merkle.root
    pay_ref = payload_ref(shard, epoch, layout, codec, version)
    support_ref = aux_ref(shard, epoch, mode, root)
    k = epoch
    meta = Meta(n=n, k=k, payload_ref=pay_ref, aux_ref=support_ref, layout=layout, mode=mode, codec=codec, version=version)
    entry = MSIEntry(root=root, meta=meta)
    openssl = Ed25519OpenSSL()
    private_seed = hashlib.sha256(b'MSI-E3-ED25519-SEED\x00' + seed.to_bytes(8, 'big', signed=False)).digest()
    public_key = openssl.public_from_seed(private_seed)
    statement = root_statement(shard, epoch, k, root)
    signature = openssl.sign(private_seed, statement)
    if not openssl.verify(public_key, statement, signature):
        raise RuntimeError('generated direct certificate failed self-verification')
    certificate = DirectCertificate(public_key=public_key, statement=statement, signature=signature)
    responses: dict[int, Response] = {}
    if retain_responses:
        for position in pool:
            q = (shard, epoch, position)
            candidate = encode_candidate_payload(selected_blocks[position], position=position, layout=layout, codec=codec, version=version, ref=pay_ref)
            if scheme == 'B3_leaf':
                witness = merkle.padded_leaves
            else:
                witness = extract_path(merkle.levels, position)
            responses[position] = Response(q=q, payload=candidate, aux_ref=support_ref, mode=mode, witness=witness, certificate=certificate)
    setup_ns = time.perf_counter_ns() - start
    metadata = {'setup_ns': setup_ns, 'rss_before_kib': rss_before, 'rss_after_kib': get_rss_kib(), 'n': n, 'n_prime': next_power_of_two(n), 'depth': next_power_of_two(n).bit_length() - 1, 'query_pool_positions': list(pool), 'root_hex': root.hex(), 'openssl_library': openssl.library, 'openssl_version': openssl.version(), 'retained_response_count': len(responses), 'retained_leaf_vector': bool(retain_responses and scheme == 'B3_leaf')}
    del merkle, leaves, selected_blocks
    gc.collect()
    return (shard, epoch, pool, entry, responses, metadata, public_key)

def build_server_fixture(case: dict[str, Any]) -> ServerFixture:
    shard, epoch, pool, entry, responses, metadata, _public_key = _build_common(case, retain_responses=True)
    return ServerFixture(shard=shard, epoch=epoch, target_position_pool=pool, entry=entry, responses=responses, setup_metadata=metadata)

def build_verifier_context(case: dict[str, Any]) -> VerifierContext:
    shard, epoch, pool, entry, _responses, metadata, public_key = _build_common(case, retain_responses=False)
    backend = Ed25519OpenSSL()
    verifier = Ed25519Verifier(backend, public_key)
    return VerifierContext(shard=shard, epoch=epoch, target_position_pool=pool, entry=entry, anchor_backend=verifier, anchor_cached_valid=True, setup_metadata=metadata)
