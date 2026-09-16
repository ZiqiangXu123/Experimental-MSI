from __future__ import annotations
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from msi_query_exp.config import load_config, make_plan, write_plan
from msi_query_exp.constants import SCHEMES
from msi_query_exp.crypto import Ed25519OpenSSL, OperationCounter
from msi_query_exp.encoding import canonical_block, decode_candidate_payload, encode_candidate_payload, parse_canonical_block, payload_ref
from msi_query_exp.fixture import build_fixture
from msi_query_exp.merkle import build_merkle, extract_path, leaf_hash, rebuild_path, verify_member
from msi_query_exp.pipeline import accept_prepared, prepare_query
from msi_query_exp.util import next_power_of_two
ROOT = Path(__file__).resolve().parents[1]

def tiny_case(scheme: str, *, n: int=8, ablation: str='safe') -> dict[str, object]:
    return {'experiment': 'E2', 'sweep': 'unit', 'trial': 0, 'block_id': 'unit-block', 'case_id': f'unit-{scheme}-{n}-{ablation}', 'scheme': scheme, 'ablation': ablation, 'epoch_length': n, 'historical_epochs': 12, 'block_bytes': 128, 'layout': 'epoch_packed', 'codec': 'raw-v1', 'version': 1, 'shard': 1, 'measured_queries': 2, 'warmup_queries': 1, 'phase_queries': 1, 'allocation_queries': 1, 'query_pool_size': min(n, 4), 'fixture_seed': 11, 'query_seed': 17}

class CoreTests(unittest.TestCase):

    def test_next_power_of_two_boundaries(self) -> None:
        expected = {1: 1, 2: 2, 3: 4, 4: 4, 5: 8, 63: 64, 64: 64, 65: 128}
        for n, n_prime in expected.items():
            self.assertEqual(next_power_of_two(n), n_prime)

    def test_canonical_payload_roundtrip_all_layout_codecs(self) -> None:
        q = (3, 7, 5)
        block = canonical_block(*q, block_bytes=256, seed=9)
        self.assertEqual(parse_canonical_block(block, q), block)
        for layout in ('per_block', 'epoch_packed'):
            for codec in ('raw-v1', 'zlib-v1'):
                ref = payload_ref(q[0], q[1], layout, codec, 1)
                payload = encode_candidate_payload(block, position=q[2], layout=layout, codec=codec, version=1, ref=ref)
                self.assertEqual(decode_candidate_payload(payload, q), block)

    def test_merkle_paths_and_leaf_reconstruction(self) -> None:
        for n in (1, 3, 8, 9):
            q_prefix = (1, 4)
            blocks = [canonical_block(1, 4, i, 128, 19) for i in range(1, n + 1)]
            leaves = [leaf_hash((1, 4, i), b) for i, b in enumerate(blocks, start=1)]
            material = build_merkle(leaves)
            self.assertEqual(len(material.padded_leaves), next_power_of_two(n))
            for position in (1, n):
                path = extract_path(material.levels, position)
                rebuilt = rebuild_path(position, material.padded_leaves)
                self.assertEqual(path, rebuilt)
                self.assertTrue(verify_member((q_prefix[0], q_prefix[1], position), blocks[position - 1], path, material.root, n))

    def test_ed25519_native_roundtrip_and_tamper_rejection(self) -> None:
        backend = Ed25519OpenSSL()
        seed = bytes(range(32))
        public = backend.public_from_seed(seed)
        message = b'MSI E2 unit certificate'
        signature = backend.sign(seed, message)
        self.assertTrue(backend.verify(public, message, signature))
        self.assertFalse(backend.verify(public, message + b'!', signature))

    def test_valid_pipeline_all_schemes(self) -> None:
        for scheme in SCHEMES:
            case = tiny_case(scheme)
            fixture = build_fixture(case)
            try:
                position = fixture.target_position_pool[0]
                prepared = prepare_query(fixture, position, case)
                self.assertIsNotNone(prepared)
                self.assertIsNotNone(accept_prepared(prepared, fixture, case))
            finally:
                fixture.anchor_backend.close()

    def test_operation_counts(self) -> None:
        expectations = {'B0_raw': (0, 0), 'B1_verified': (4, 1), 'B2_full': (4, 1), 'B3_leaf': (12, 1), 'B4_ext': (4, 1)}
        for scheme, expected in expectations.items():
            case = tiny_case(scheme)
            fixture = build_fixture(case)
            try:
                prepared = prepare_query(fixture, fixture.target_position_pool[0], case)
                self.assertIsNotNone(prepared)
                counter = OperationCounter()
                self.assertIsNotNone(accept_prepared(prepared, fixture, case, counter))
                self.assertEqual((counter.hash_calls, counter.signature_verifications), expected)
            finally:
                fixture.anchor_backend.close()

    def test_checkresp_rejects_mismatched_response(self) -> None:
        case = tiny_case('B2_full')
        fixture = build_fixture(case)
        try:
            position = fixture.target_position_pool[0]
            prepared = prepare_query(fixture, position, case)
            self.assertIsNotNone(prepared)
            assert prepared is not None and prepared.response is not None
            bad = replace(prepared.response, q=(fixture.shard, fixture.epoch, position + 1))
            tampered = replace(prepared, response=bad)
            self.assertIsNone(accept_prepared(tampered, fixture, case))
            bad_certificate = replace(prepared.response.certificate, public_key=bytes(32))
            bad_response = replace(prepared.response, certificate=bad_certificate)
            self.assertIsNone(accept_prepared(replace(prepared, response=bad_response), fixture, case))
            bad_payload = replace(prepared.response.payload, logical_offset=prepared.response.payload.logical_offset + 1)
            bad_response = replace(prepared.response, payload=bad_payload)
            self.assertIsNone(accept_prepared(replace(prepared, response=bad_response), fixture, case))
        finally:
            fixture.anchor_backend.close()

    def test_publication_plan_exact_size_and_replication(self) -> None:
        config = load_config(ROOT / 'configs' / 'publication.json')
        blocks = make_plan(config)
        self.assertEqual(len(blocks), 290)
        self.assertEqual(sum((len(block['cases']) for block in blocks)), 1330)
        with tempfile.TemporaryDirectory() as tmp:
            summary = write_plan(Path(tmp) / 'plan.jsonl', blocks)
        self.assertEqual(summary['minimum_launches_per_configuration'], 10)
        self.assertEqual(summary['maximum_launches_per_configuration'], 10)
if __name__ == '__main__':
    unittest.main(verbosity=2)
