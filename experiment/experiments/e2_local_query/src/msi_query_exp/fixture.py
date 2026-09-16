from __future__ import annotations
import hashlib
import random
import time
from typing import Any
from .constants import MODE_BY_SCHEME
from .crypto import Ed25519OpenSSL, Ed25519Verifier
from .encoding import aux_ref, canonical_block, encode_candidate_payload, payload_ref, root_statement
from .merkle import build_merkle, extract_path, leaf_hash
from .models import DirectCertificate, Fixture, MSIEntry, Meta, Response
from .util import get_rss_kib, next_power_of_two, stable_seed

def query_position_pool(n: int, pool_size: int, seed: int) -> tuple[int, ...]:
    if n < 1:
        raise ValueError('n must be positive')
    if pool_size < 1:
        raise ValueError('query pool size must be positive')
    if n <= pool_size:
        return tuple(range(1, n + 1))
    positions = {1, 2, n, n - 1, max(1, n // 2), min(n, n // 2 + 1), max(1, n // 4), min(n, 3 * n // 4)}
    rng = random.Random(seed)
    while len(positions) < min(pool_size, n):
        positions.add(rng.randint(1, n))
    return tuple(sorted(positions))

def build_fixture(case: dict[str, Any]) -> Fixture:
    start = time.perf_counter_ns()
    rss_before = get_rss_kib()
    scheme = str(case['scheme'])
    mode = MODE_BY_SCHEME[scheme]
    n = int(case['epoch_length'])
    historical_epochs = int(case['historical_epochs'])
    block_bytes = int(case['block_bytes'])
    layout = str(case['layout'])
    codec = str(case['codec'])
    version = int(case.get('version', 1))
    seed = int(case['fixture_seed'])
    shard = int(case.get('shard', 1))
    epoch = max(1, historical_epochs)
    target_pool = query_position_pool(n, int(case['query_pool_size']), int(case['query_seed']))
    selected = set(target_pool)
    retain_local_archive = scheme in {'B0_raw', 'B1_verified'}
    local_blocks: dict[int, bytes] | None = {} if retain_local_archive else None
    selected_blocks: dict[int, bytes] = {}
    leaves: list[bytes] = []
    for position in range(1, n + 1):
        block = canonical_block(shard, epoch, position, block_bytes, seed)
        leaves.append(leaf_hash((shard, epoch, position), block))
        if retain_local_archive:
            assert local_blocks is not None
            local_blocks[position] = block
        if position in selected:
            selected_blocks[position] = block
    merkle = build_merkle(leaves)
    root = merkle.root
    pay_ref = payload_ref(shard, epoch, layout, codec, version)
    metadata_mode = 'full' if scheme == 'B0_raw' else mode
    support_ref = aux_ref(shard, epoch, metadata_mode, root)
    k = epoch
    meta = Meta(n=n, k=k, payload_ref=pay_ref, aux_ref=support_ref, layout=layout, mode=metadata_mode, codec=codec, version=version)
    entry = MSIEntry(root=root, meta=meta)
    index = {t: entry for t in range(1, historical_epochs + 1)}
    index[epoch] = entry
    openssl = Ed25519OpenSSL()
    private_seed = hashlib.sha256(b'MSI-E2-ED25519-SEED\x00' + seed.to_bytes(8, 'big', signed=False)).digest()
    public_key = openssl.public_from_seed(private_seed)
    statement = root_statement(shard, epoch, k, root)
    signature = openssl.sign(private_seed, statement)
    certificate = DirectCertificate(public_key=public_key, statement=statement, signature=signature)
    verifier = Ed25519Verifier(openssl, public_key)
    if not verifier.verify(statement, signature):
        raise RuntimeError('generated direct certificate failed self-verification')
    responses: dict[int, Response] = {}
    for position in target_pool:
        q = (shard, epoch, position)
        candidate = encode_candidate_payload(selected_blocks[position], position=position, layout=layout, codec=codec, version=version, ref=pay_ref)
        if scheme == 'B3_leaf':
            witness = merkle.padded_leaves
        elif scheme == 'B0_raw':
            witness = tuple()
        else:
            witness = extract_path(merkle.levels, position)
        responses[position] = Response(q=q, payload=candidate, aux_ref=support_ref, mode=metadata_mode, witness=witness, certificate=certificate)
    if scheme in {'B0_raw', 'B1_verified'}:
        retained_levels = merkle.levels
    else:
        retained_levels = None
    retained_vector = merkle.padded_leaves if scheme == 'B3_leaf' else None
    setup_ns = time.perf_counter_ns() - start
    rss_after = get_rss_kib()
    return Fixture(shard=shard, epoch=epoch, target_position_pool=target_pool, index=index, entry=entry, responses=responses, local_blocks=local_blocks, full_tree_levels=retained_levels, leaf_vector=retained_vector, anchor_backend=verifier, anchor_cached_valid=True, setup_metadata={'setup_ns': setup_ns, 'rss_before_kib': rss_before, 'rss_after_kib': rss_after, 'n': n, 'n_prime': next_power_of_two(n), 'depth': next_power_of_two(n).bit_length() - 1, 'query_pool_positions': list(target_pool), 'retained_local_blocks': len(local_blocks) if local_blocks is not None else 0, 'retained_full_levels': retained_levels is not None, 'retained_leaf_vector': retained_vector is not None, 'root_hex': root.hex(), 'openssl_library': openssl.library, 'openssl_version': openssl.version()})
