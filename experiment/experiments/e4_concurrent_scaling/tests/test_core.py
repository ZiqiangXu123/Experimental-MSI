from __future__ import annotations
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from msi_scaling_exp.analysis import inspect_raw, missing_case_indices
from msi_scaling_exp.benchmark import control, run_case
from msi_scaling_exp.config import make_plan
from msi_scaling_exp.crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from msi_scaling_exp.index import DatasetSpec, IndexReader, build_dataset, dataset_paths, hot_indices, index_to_key
from msi_scaling_exp.merkle import apply_path, build_root, canonical_payload, hot_fixture, leaf_hash, path_from_leaves, root_statement, verify_member
from msi_scaling_exp.service import ServiceConfig, serve
from msi_scaling_exp.util import jsonl_iter
from msi_scaling_exp.wire import KIND_QUERY, KIND_SHUTDOWN, MODE_FULL, Request, Response, STATUS_OK, decode_path, decode_request, decode_response, encode_path, encode_request, encode_response, recv_frame, send_frame
from msi_scaling_exp.workload import QuerySampler
ROOT = Path(__file__).resolve().parents[1]

def tiny_case() -> dict[str, object]:
    return {'schema_version': 1, 'experiment': 'E4', 'case_id': 'unit-e4-t00', 'config_id': 'unit-e4', 'trial': 0, 'seed': 40404, 'primary_sweep': 'unit', 'sweep_tags': ['unit'], 'shard_count': 2, 'historical_epochs': 4, 'epoch_length': 8, 'block_bytes': 128, 'fixture_seed': 44012026, 'hotset_size': 8, 'mode': 'full', 'distribution': 'uniform', 'concurrent_clients': 4, 'verifier_workers': 2, 'service_cpus': 4, 'target_rtt_ms': 0.2, 'bandwidth_mbps': 1000.0, 'anchor_key_seed': 44022026, 'trial_kind': 'steady', 'rate_multipliers': [0.5], 'steady_multiplier': 0.5, 'calibration_s': 0.08, 'warmup_s': 0.03, 'measure_s': 0.1, 'max_offered_qps': 10000.0, 'rtt_probe_count': 3, 'sample_interval_s': 0.02, 'burst_period_s': 0.05, 'pin_workers': False}

class CoreTests(unittest.TestCase):

    def test_01_ed25519_roundtrip_and_tamper(self) -> None:
        signer = Ed25519Signer(deterministic_seed('unit', 7))
        verifier = Ed25519Verifier(signer.public_key)
        try:
            message = b'MSI E4 unit test'
            signature = signer.sign(message)
            self.assertEqual(len(signature), 64)
            self.assertTrue(verifier.verify(message, signature))
            self.assertFalse(verifier.verify(message + b'!', signature))
        finally:
            signer.close()
            verifier.close()

    def test_02_merkle_full_and_leaf_witnesses(self) -> None:
        for n in (1, 2, 8, 16):
            payload, path, leaves, root = hot_fixture(3, 11, n, 128, 19)
            self.assertEqual(build_root(leaves), root)
            self.assertEqual(path_from_leaves(0, leaves), path)
            self.assertTrue(verify_member(3, 11, 0, payload, path, root))
            self.assertEqual(apply_path(leaf_hash(3, 11, 0, payload), path), root)
            self.assertFalse(verify_member(3, 11, 0, payload[:-1] + b'x', path, root))

    def test_03_wire_roundtrip_and_crc_rejection(self) -> None:
        request = Request(KIND_QUERY, MODE_FULL, 91, 3, 7)
        self.assertEqual(decode_request(encode_request(request)), request)
        path = ((bytes(range(32)), 1), (bytes(reversed(range(32))), 0))
        self.assertEqual(decode_path(encode_path(path), 2), path)
        response = Response(KIND_QUERY, STATUS_OK, MODE_FULL, 91, 3, 7, 8, 10, 11, bytes(32), bytes(128), encode_path(path), bytes(64))
        encoded = encode_response(response)
        self.assertEqual(decode_response(encoded), response)
        damaged = bytearray(encoded)
        damaged[-1] ^= 1
        with self.assertRaises(ValueError):
            decode_response(bytes(damaged))

    def test_04_fixed_width_index_build_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = DatasetSpec(3, 5, 8, 128, 123, 6)
            result = build_dataset(tmp, spec, workers=2)
            self.assertEqual(result['status'], 'PASS')
            path, _ = dataset_paths(tmp, spec)
            self.assertEqual(path.stat().st_size, 4096 + spec.total_records * 64)
            with IndexReader(path) as reader:
                for shard, epoch in ((0, 0), (1, 3), (2, 4)):
                    record, elapsed = reader.lookup(shard, epoch)
                    self.assertEqual(record['n'], 8)
                    self.assertGreaterEqual(elapsed, 0)
                    self.assertEqual(len(record['root']), 32)

    def test_05_fixture_seed_is_not_trial_seed(self) -> None:
        base = tiny_case()
        a = DatasetSpec.from_dict({**base, 'seed': 1})
        b = DatasetSpec.from_dict({**base, 'seed': 999})
        self.assertEqual(a, b)
        self.assertEqual(a.seed, int(base['fixture_seed']))

    def test_06_workload_generators_stay_in_range(self) -> None:
        spec = DatasetSpec(8, 100, 8, 128, 1, 32)
        hot = set(hot_indices(spec))
        for distribution in ('uniform', 'zipf0.8', 'zipf1.2', 'recency', 'bursty'):
            sampler = QuerySampler(spec, distribution, 17, False, 0.05)
            for i in range(200):
                shard, epoch = sampler.sample(i / 100.0)
                self.assertTrue(0 <= shard < spec.shard_count)
                self.assertTrue(0 <= epoch < spec.historical_epochs)
            leaf_sampler = QuerySampler(spec, distribution, 17, True, 0.05)
            for i in range(100):
                key = leaf_sampler.sample(i / 100.0)
                self.assertIn(key[0] * spec.historical_epochs + key[1], hot)

    def test_07_real_tcp_service_returns_verifiable_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ready = tmp_path / 'ready.json'
            final = tmp_path / 'final.json'
            spec = DatasetSpec(2, 4, 8, 128, 31, 8)
            cfg = ServiceConfig(spec, 1000.0, 4, 99)
            thread = threading.Thread(target=serve, args=(cfg, '127.0.0.1', 0, ready, final, '127.0.0.1'), daemon=True)
            thread.start()
            deadline = time.time() + 10
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.01)
            info = json.loads(ready.read_text())
            with socket.create_connection((info['host'], info['port']), timeout=5) as sock:
                request = Request(KIND_QUERY, MODE_FULL, 77, 1, 3)
                send_frame(sock, encode_request(request))
                response = decode_response(recv_frame(sock))
            self.assertEqual(response.status, STATUS_OK)
            payload = canonical_payload(1, 3, 128, spec.seed, 0)
            self.assertEqual(response.payload, payload)
            self.assertTrue(verify_member(1, 3, 0, response.payload, decode_path(response.witness, 3), response.root))
            verifier = Ed25519Verifier(bytes.fromhex(info['public_key_hex']))
            try:
                self.assertTrue(verifier.verify(root_statement(1, 3, 8, response.root), response.signature))
            finally:
                verifier.close()
            control(info['host'], info['port'], KIND_SHUTDOWN, 88)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_08_tiny_end_to_end_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            case = tiny_case()
            spec = DatasetSpec.from_dict(case)
            build_dataset(base / 'datasets', spec, workers=1)
            result = run_case(case, base / 'datasets', base / 'raw')
            self.assertTrue(result['checks']['all_queries_accepted'])
            self.assertTrue(result['checks']['hash_calls_exact'])
            self.assertEqual(result['checks']['expected_hash_calls_per_query'], 4)
            self.assertEqual(len(result['rate_steps']), 1)
            self.assertGreater(result['rate_steps'][0]['accepted_total'], 0)

    def test_09_publication_plan_exact_size_and_replication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.jsonl'
            summary = make_plan(ROOT / 'configs' / 'publication.json', plan)
            self.assertEqual(summary['configurations'], 80)
            self.assertEqual(summary['cases'], 400)
            self.assertEqual(summary['datasets'], 10)
            rows = list(jsonl_iter(plan))
            self.assertEqual(sum((len(r['rate_multipliers']) if r['trial_kind'] == 'ramp' else 1 for r in rows)), 975)
            by_config: dict[str, int] = {}
            for row in rows:
                by_config[row['config_id']] = by_config.get(row['config_id'], 0) + 1
            self.assertEqual(set(by_config.values()), {5})

    def test_10_raw_audit_reports_missing_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plan = base / 'plan.jsonl'
            make_plan(ROOT / 'configs' / 'smoke.json', plan)
            audit = inspect_raw(plan, base / 'raw')
            self.assertFalse(audit['complete'])
            self.assertEqual(len(audit['missing_case_ids']), 5)
            self.assertEqual(missing_case_indices(plan, base / 'raw'), list(range(5)))
if __name__ == '__main__':
    unittest.main(verbosity=2)
