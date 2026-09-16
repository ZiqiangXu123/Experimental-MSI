from __future__ import annotations
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from msi_network_exp.benchmark import run_network_case
from msi_network_exp.config import load_config, make_plan, write_plan
from msi_network_exp.constants import MSG_QUERY_COALESCED, MSG_QUERY_PAYLOAD, MSG_QUERY_WITNESS
from msi_network_exp.crypto import Ed25519OpenSSL, OperationCounter
from msi_network_exp.fixture import build_server_fixture, build_verifier_context
from msi_network_exp.link import LinkProfile, sample_transfer
from msi_network_exp.merkle import build_merkle, extract_path, leaf_hash, rebuild_path, verify_member
from msi_network_exp.pipeline import accept_response
from msi_network_exp.service import run_service
from msi_network_exp.util import next_power_of_two, read_json
from msi_network_exp.wire import decode_coalesced_response, decode_frame_bytes, decode_payload_response, decode_witness_response, encode_coalesced_response, encode_payload_response, encode_query, encode_witness_response, merge_split_response
ROOT = Path(__file__).resolve().parents[1]

def tiny_case(scheme: str, deployment: str='coalesced', profile: str='P0') -> dict[str, object]:
    return {'schema_version': 1, 'experiment': 'E3', 'kind': 'network_trial', 'sweep': 'unit', 'trial': 0, 'block_id': 'unit-block', 'case_id': f'unit-{scheme}-{deployment}-{profile}', 'scheme': scheme, 'deployment': deployment, 'profile': profile, 'epoch_length': 9, 'historical_epochs': 12, 'block_bytes': 128, 'layout': 'epoch_packed', 'codec': 'raw-v1', 'version': 1, 'shard': 1, 'measured_queries': 1, 'warmup_queries': 1, 'query_pool_size': 4, 'calibration_pings': 3, 'socket_timeout_s': 30.0, 'fixture_seed': 11, 'query_seed': 17, 'network_seed': 23, 'deadlines_ms': [50, 100, 250, 500]}

class E3CoreTests(unittest.TestCase):

    def test_next_power_of_two_boundaries(self) -> None:
        expected = {1: 1, 2: 2, 3: 4, 8: 8, 9: 16, 63: 64, 64: 64, 65: 128}
        for n, n_prime in expected.items():
            self.assertEqual(next_power_of_two(n), n_prime)

    def test_merkle_paths_and_algorithm_s1(self) -> None:
        from msi_network_exp.encoding import canonical_block
        for n in (1, 3, 8, 9):
            blocks = [canonical_block(1, 4, i, 128, 19) for i in range(1, n + 1)]
            leaves = [leaf_hash((1, 4, i), block) for i, block in enumerate(blocks, 1)]
            material = build_merkle(leaves)
            for position in (1, n):
                path = extract_path(material.levels, position)
                self.assertEqual(path, rebuild_path(position, material.padded_leaves))
                self.assertTrue(verify_member((1, 4, position), blocks[position - 1], path, material.root, n))

    def test_native_ed25519(self) -> None:
        backend = Ed25519OpenSSL()
        seed = bytes(range(32))
        public = backend.public_from_seed(seed)
        message = b'MSI E3 certificate'
        signature = backend.sign(seed, message)
        self.assertTrue(backend.verify(public, message, signature))
        self.assertFalse(backend.verify(public, message + b'!', signature))

    def test_wire_roundtrip_and_acceptance_all_variants(self) -> None:
        variants = [('B2_full', 'coalesced'), ('B3_leaf', 'coalesced'), ('B4_ext', 'coalesced'), ('B4_ext', 'split')]
        for scheme, deployment in variants:
            case = tiny_case(scheme, deployment)
            server = build_server_fixture(case)
            verifier = build_verifier_context(case)
            try:
                response = server.responses[server.target_position_pool[0]]
                if deployment == 'coalesced':
                    raw, _breakdown = encode_coalesced_response(5, server.entry, response)
                    rid, decoded, _meta = decode_coalesced_response(decode_frame_bytes(raw))
                else:
                    praw, _ = encode_payload_response(5, server.entry, response)
                    wraw, _ = encode_witness_response(5, server.entry, response)
                    rid, decoded, _meta = merge_split_response(decode_payload_response(decode_frame_bytes(praw)), decode_witness_response(decode_frame_bytes(wraw)))
                self.assertEqual(rid, 5)
                self.assertIsNotNone(accept_response(verifier, decoded, case))
            finally:
                verifier.anchor_backend.close()

    def test_wire_crc_rejects_corruption(self) -> None:
        case = tiny_case('B2_full')
        fixture = build_server_fixture(case)
        response = fixture.responses[fixture.target_position_pool[0]]
        raw, _ = encode_coalesced_response(1, fixture.entry, response)
        corrupted = bytearray(raw)
        corrupted[-1] ^= 1
        with self.assertRaises(ValueError):
            decode_frame_bytes(bytes(corrupted))

    def test_operation_counts(self) -> None:
        expected = {'B2_full': 5, 'B3_leaf': 21, 'B4_ext': 5}
        for scheme, hashes in expected.items():
            case = tiny_case(scheme)
            server = build_server_fixture(case)
            verifier = build_verifier_context(case)
            try:
                response = server.responses[server.target_position_pool[0]]
                counter = OperationCounter()
                self.assertIsNotNone(accept_response(verifier, response, case, counter))
                self.assertEqual(counter.hash_calls, hashes)
                self.assertEqual(counter.signature_verifications, 1)
            finally:
                verifier.anchor_backend.close()

    def test_link_model_deterministic_and_eventually_delivers(self) -> None:
        profile = LinkProfile.from_id('P3')
        a = sample_transfer(profile, 500000, baseline_rtt_ns=100000, seed=7, endpoint='gateway', direction='response', request_id=1, transaction_index=0)
        b = sample_transfer(profile, 500000, baseline_rtt_ns=100000, seed=7, endpoint='gateway', direction='response', request_id=1, transaction_index=0)
        self.assertEqual(a, b)
        self.assertGreaterEqual(a.virtual_wire_bytes, a.application_bytes)
        self.assertGreaterEqual(a.total_delay_ns, a.serialization_delay_ns)

    def test_small_socket_integration_split(self) -> None:
        case = tiny_case('B4_ext', 'split', 'P0')
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            threads: list[threading.Thread] = []
            endpoints = {}
            metric_files = {}
            for role in ('payload', 'witness'):
                ready = tmpdir / f'{role}.ready.json'
                metrics = tmpdir / f'{role}.metrics.json.gz'
                metric_files[role] = str(metrics)
                thread = threading.Thread(target=run_service, kwargs={'case': case, 'role': role, 'host': '127.0.0.1', 'port': 0, 'ready_file': ready, 'metrics_file': metrics, 'expected_queries': 2, 'accept_timeout_s': 10, 'io_timeout_s': 30}, daemon=True)
                thread.start()
                threads.append(thread)
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                endpoints[role] = read_json(ready)
            result = run_network_case(case, endpoints, metric_files)
            self.assertTrue(all(result['checks'].values()))
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())
            for metrics in metric_files.values():
                self.assertTrue(all(read_json(Path(metrics))['checks'].values()))

    def test_publication_plan_exact_size(self) -> None:
        config = load_config(ROOT / 'configs' / 'publication.json')
        blocks = make_plan(config)
        self.assertEqual(len(blocks), 156)
        self.assertEqual(sum((len(block['cases']) for block in blocks)), 624)
        with tempfile.TemporaryDirectory() as tmp:
            summary = write_plan(Path(tmp) / 'plan.jsonl', blocks)
        self.assertEqual(summary['wire_audit_cases'], 64)
        self.assertEqual(summary['network_trial_cases'], 560)
        self.assertEqual(summary['minimum_network_trials_per_configuration'], 5)
        self.assertEqual(summary['maximum_network_trials_per_configuration'], 5)
        self.assertEqual(summary['total_measured_network_queries'], 112000)
if __name__ == '__main__':
    unittest.main(verbosity=2)
