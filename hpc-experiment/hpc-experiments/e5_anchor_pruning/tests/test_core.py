from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from msi_anchor_exp.accumulator import AggregateAnchor, DirectAnchor, DynamicAccumulator, HashCounter, RootStatement, aggregate_evidence_bytes_for_power_of_two, deterministic_statement, direct_evidence_bytes, make_aggregate_evidence, make_checkpoint_anchor, make_direct_evidence, verify_aggregate_evidence, verify_direct_evidence
from msi_anchor_exp.benchmark import run_case
from msi_anchor_exp.config import count_plan, make_plan
from msi_anchor_exp.crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from msi_anchor_exp.dataset import DatasetSpec, MappedAccumulatorDataset, build_dataset, dataset_path, validate_dataset
from msi_anchor_exp.epoch import commit_epoch
from msi_anchor_exp.pruning import simulate_pruning
from msi_anchor_exp.util import read_json_gz

class E5CoreTests(unittest.TestCase):

    def _pruning_case(self, **overrides):
        case = {'case_id': 'test-prune', 'config_id': 'test', 'trial': 0, 'trial_kind': 'pruning', 'policy': 'safe', 'anchor_mode': 'aggregate', 'checkpoint_interval': 8, 'seed': 1234, 'shard': 1, 'anchor_key_seed': 5052026, 'total_epochs': 32, 'blocks_per_epoch': 4, 'block_size': 128, 'arrival_rate_eps': 100, 'finality_delay_ms': 10.0, 'certificate_delay_ms': 20.0, 'retry_delay_ms': 20.0, 'hot_window_epochs': 16, 'fault': 'none', 'fault_fraction': 0.25, 'timeline_sample_limit': 100}
        case.update(overrides)
        return case

    def test_root_statement_round_trip_and_binding(self):
        statement = deterministic_statement(7, 3, 9)
        self.assertEqual(RootStatement.decode(statement.encode()), statement)
        altered = RootStatement(statement.shard, statement.epoch, statement.position + 1, statement.root)
        self.assertNotEqual(altered.encode(), statement.encode())

    def test_direct_evidence_accepts_valid_and_rejects_tamper(self):
        signer = Ed25519Signer(deterministic_seed('test', 1))
        verifier = Ed25519Verifier(signer.public_key)
        try:
            statement = deterministic_statement(11, 1, 5)
            evidence = make_direct_evidence(statement, signer)
            anchor = DirectAnchor(1, signer.public_key)
            self.assertEqual(len(evidence), direct_evidence_bytes())
            self.assertTrue(verify_direct_evidence(statement, evidence, anchor, verifier))
            corrupted = evidence[:-1] + bytes([evidence[-1] ^ 1])
            self.assertFalse(verify_direct_evidence(statement, corrupted, anchor, verifier))
        finally:
            signer.close()
            verifier.close()

    def test_aggregate_proof_is_position_bound(self):
        accumulator = DynamicAccumulator()
        statements = [deterministic_statement(17, 1, position) for position in range(1, 65)]
        for statement in statements:
            accumulator.append_statement(statement)
        signer = Ed25519Signer(deterministic_seed('test', 2))
        verifier = Ed25519Verifier(signer.public_key)
        try:
            anchor = make_checkpoint_anchor(1, 64, accumulator.root(64), signer)
            statement = statements[12]
            proof = accumulator.proof(statement.position, 64)
            evidence = make_aggregate_evidence(statement, proof)
            counter = HashCounter()
            self.assertTrue(verify_aggregate_evidence(statement, evidence, anchor, verifier, counter))
            self.assertEqual(counter.total, 8)
            self.assertEqual(len(evidence), aggregate_evidence_bytes_for_power_of_two(64))
            wrong = statements[13]
            self.assertFalse(verify_aggregate_evidence(wrong, evidence, anchor, verifier, HashCounter()))
        finally:
            signer.close()
            verifier.close()

    def test_mmap_dataset_matches_dynamic_accumulator(self):
        with tempfile.TemporaryDirectory() as temporary:
            spec = DatasetSpec(max_prefix=64, shard=1, statement_seed=99)
            result = build_dataset(temporary, spec)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(validate_dataset(dataset_path(temporary, spec), spec, verify_hashes=True)['status'], 'PASS')
            accumulator = DynamicAccumulator()
            for position in range(1, 65):
                accumulator.append_statement(deterministic_statement(99, 1, position))
            with MappedAccumulatorDataset(dataset_path(temporary, spec)) as mapped:
                self.assertEqual(mapped.power_of_two_root(64), accumulator.root(64))
                self.assertEqual(mapped.proof_power_of_two(37, 64), accumulator.proof(37, 64))

    def test_epoch_commitment_is_deterministic_and_counts_padding(self):
        root_a, metrics_a = commit_epoch(5, 1, 2, 5, 128)
        root_b, metrics_b = commit_epoch(5, 1, 2, 5, 128)
        self.assertEqual(root_a, root_b)
        self.assertEqual(metrics_a.hash_calls, metrics_b.hash_calls)
        self.assertEqual(metrics_a.leaf_hashes, 5)
        self.assertEqual(metrics_a.internal_hashes, 7)

    def test_safe_pruning_has_zero_gate_violations(self):
        result = simulate_pruning(self._pruning_case())
        self.assertEqual(result['safety']['total_gate_violations'], 0)
        self.assertEqual(result['counts']['pending_epochs'], 0)
        self.assertTrue(result['checks']['all_internal_checks_pass'])

    def test_invalid_signature_fails_closed_and_recovers(self):
        result = simulate_pruning(self._pruning_case(fault='invalid_signature', fault_fraction=1.0))
        self.assertGreater(result['counts']['fault_injections'], 0)
        self.assertGreater(result['counts']['fault_rejections'], 0)
        self.assertEqual(result['safety']['total_gate_violations'], 0)
        self.assertTrue(result['checks']['all_internal_checks_pass'])

    def test_missing_certificate_retains_hot_copy(self):
        result = simulate_pruning(self._pruning_case(fault='missing_certificate', fault_fraction=1.0))
        self.assertGreater(result['counts']['checkpoint_dropped'], 0)
        self.assertGreater(result['backlog']['max_retained_epochs'], 0)
        self.assertEqual(result['safety']['total_gate_violations'], 0)

    def test_unsafe_counterexample_exhibits_gate_violation(self):
        result = simulate_pruning(self._pruning_case(trial_kind='unsafe_counterexample', policy='unsafe_delete_after_upload', total_epochs=10))
        self.assertGreater(result['safety']['total_gate_violations'], 0)
        self.assertTrue(result['checks']['unsafe_counterexample_exhibits_violation'])

    def test_plan_counts_are_frozen(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            smoke_plan = Path(temporary) / 'smoke.jsonl'
            public_plan = Path(temporary) / 'publication.jsonl'
            smoke = make_plan(root / 'configs' / 'smoke.json', smoke_plan)
            publication = make_plan(root / 'configs' / 'publication.json', public_plan)
            self.assertEqual((smoke['configurations'], smoke['cases']), (15, 15))
            self.assertEqual((publication['configurations'], publication['cases']), (80, 552))
            self.assertEqual(count_plan(public_plan, 'query_measurements'), 1200000)
            self.assertEqual(count_plan(public_plan, 'pruned_epoch_inputs'), 460400)

    def test_end_to_end_anchor_query_and_update_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw = base / 'raw'
            raw.mkdir()
            spec = DatasetSpec(max_prefix=64, shard=1, statement_seed=77)
            build_dataset(base / 'datasets', spec)
            common = {'schema_version': 1, 'experiment': 'E5', 'config_id': 'tiny', 'trial': 0, 'seed': 123, 'max_prefix': 64, 'shard': 1, 'statement_seed': 77, 'anchor_key_seed': 5052026, 'disable_pinning': True}
            query = {**common, 'case_id': 'tiny-query', 'trial_kind': 'anchor_query', 'anchor_mode': 'aggregate', 'prefix_count': 64, 'warmup_queries': 2, 'measured_queries': 10, 'accounting_checkpoint_interval': 8}
            update = {**common, 'case_id': 'tiny-update', 'trial_kind': 'checkpoint_update', 'anchor_mode': 'aggregate', 'target_prefix': 64, 'checkpoint_interval': 8, 'warmup_updates': 1, 'measured_updates': 3}
            for case in (query, update):
                result = run_case(case, base / 'datasets', raw)
                self.assertTrue(result['checks']['all_internal_checks_pass'])
                persisted = read_json_gz(raw / f"{case['case_id']}.json.gz")
                self.assertEqual(persisted['case']['case_id'], case['case_id'])
if __name__ == '__main__':
    unittest.main()
