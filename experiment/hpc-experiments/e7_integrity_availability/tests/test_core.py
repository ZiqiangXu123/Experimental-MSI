from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from msi_security_avail_exp.analysis import analyze, validate_results
from msi_security_avail_exp.attacks import build_attack_environment, make_ablation_attack, make_attack
from msi_security_avail_exp.availability import build_fixture_audit, run_availability_case
from msi_security_avail_exp.constants import ATTACK_FAMILIES
from msi_security_avail_exp.e7_runner import run_block as run_e7_block
from msi_security_avail_exp.availability import run_block as run_e8_block
from msi_security_avail_exp.fixture import build_fixture, honest_frame
from msi_security_avail_exp.pipeline import verify_frame
from msi_security_avail_exp.plan import build_plan
from msi_security_avail_exp.protocol import decode_response
from msi_security_avail_exp.util import read_jsonl
ROOT = Path(__file__).resolve().parents[1]

class CoreTests(unittest.TestCase):

    def test_honest_direct_full_accepts(self) -> None:
        case = {'epoch_length': 64, 'block_bytes': 512, 'mode': 'full', 'anchor_mode': 'direct', 'layout': 'per_block', 'codec': 'raw-v1', 'query_position': 32, 'fixture_seed': 1}
        fixture = build_fixture(case)
        q = (fixture.shard, fixture.epoch, 32)
        decision = verify_frame(honest_frame(fixture, 32), q, fixture)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.stage, 'Accept')

    def test_honest_aggregate_ext_accepts(self) -> None:
        case = {'epoch_length': 64, 'block_bytes': 512, 'mode': 'ext', 'anchor_mode': 'aggregate', 'anchor_prefix': 64, 'layout': 'per_block', 'codec': 'raw-v1', 'query_position': 32, 'fixture_seed': 2}
        fixture = build_fixture(case)
        q = (fixture.shard, fixture.epoch, 32)
        decision = verify_frame(honest_frame(fixture, 32), q, fixture)
        self.assertTrue(decision.accepted)
        self.assertGreater(decision.hash_calls, 0)

    def test_honest_leaf_rebuild_accepts(self) -> None:
        case = {'epoch_length': 65, 'block_bytes': 512, 'mode': 'leaf', 'anchor_mode': 'direct', 'layout': 'epoch_packed', 'codec': 'zlib-v1', 'query_position': 33, 'fixture_seed': 3}
        fixture = build_fixture(case)
        q = (fixture.shard, fixture.epoch, 33)
        parsed = decode_response(honest_frame(fixture, 33))
        self.assertEqual(len(parsed.witness), 128)
        self.assertTrue(verify_frame(honest_frame(fixture, 33), q, fixture).accepted)

    def test_all_attack_families_fail_closed(self) -> None:
        case = {'epoch_length': 64, 'block_bytes': 512, 'mode': 'full', 'anchor_mode': 'direct', 'layout': 'per_block', 'codec': 'raw-v1', 'query_position': 32, 'fixture_seed': 4}
        env = build_attack_environment(case)
        for family in ATTACK_FAMILIES:
            mutation = make_attack(env, family, 0)
            for frame, expected in zip(mutation.frames, mutation.expected_stages):
                decision = verify_frame(frame, mutation.query, env.target)
                self.assertFalse(decision.accepted, family)
                self.assertNotEqual(decision.stage, 'Crash', family)
                if expected is not None:
                    self.assertEqual(decision.stage, expected, family)

    def test_unsafe_ablations_are_causal(self) -> None:
        case = {'epoch_length': 64, 'block_bytes': 512, 'mode': 'full', 'anchor_mode': 'direct', 'layout': 'per_block', 'codec': 'raw-v1', 'query_position': 32, 'fixture_seed': 5}
        env = build_attack_environment(case)
        for ablation in ('no_ctx', 'no_anchor', 'no_meta'):
            mutation = make_ablation_attack(env, ablation)
            fixture = mutation.unsafe_fixture or env.target
            for frame in mutation.frames:
                self.assertFalse(verify_frame(frame, mutation.query, fixture, ablation='safe').accepted)
                self.assertTrue(verify_frame(frame, mutation.query, fixture, ablation=mutation.unsafe_ablation or ablation).accepted)

    def test_availability_independent_matches_product(self) -> None:
        config = {'verification_warmups': 1, 'verification_audit_queries': 2, 'bandwidth_mbps': 100.0, 'latency_cv': 0.0, 'correlated_fraction': 0.7, 'request_bytes_per_service_attempt': 32, 'retry_backoff_ms': 5.0, 'retry_first_attempt_fraction': 0.45, 'service_base_latency_ms': {}}
        case = {'case_id': 'e8-test', 'kind': 'availability', 'epoch_length': 64, 'block_bytes': 512, 'mode': 'full', 'variant': 'full_colocated', 'failure_probability': 0.1, 'outage_model': 'independent', 'deadline_ms': 500, 'retry_policy': 'no_retry', 'replicate': 0, 'queries': 20000, 'anchor_mode': 'direct', 'layout': 'per_block', 'codec': 'raw-v1', 'query_position': 32, 'fixture_seed': 6, 'seed': 1234}
        audit = build_fixture_audit(case, config)
        result = run_availability_case(case, config, audit)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['metrics']['false_accepts'], 0)
        self.assertAlmostEqual(result['metrics']['completion_rate'], 0.81, delta=0.02)

    def test_publication_plan_fixed_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = build_plan(ROOT / 'configs/publication.json', Path(tmp))
        self.assertEqual(summary['e7_blocks'], 247)
        self.assertEqual(summary['e7_attack_scenarios'], 1000000)
        self.assertEqual(summary['e8_cases'], 7200)
        self.assertEqual(summary['e8_blocks'], 360)
        self.assertEqual(summary['e8_simulated_queries'], 72000000)
        self.assertTrue(all((value == 100000 for value in summary['e7_scenarios_by_family'].values())))

    def test_tiny_end_to_end_and_manifest(self) -> None:
        smoke = json.loads((ROOT / 'configs/smoke.json').read_text())
        smoke['e7']['contexts'] = smoke['e7']['contexts'][:1]
        smoke['e7']['fuzz_contexts'] = smoke['e7']['fuzz_contexts'][:1]
        smoke['e7']['deterministic_trials_per_block'] = 2
        smoke['e7']['fuzz_trials_per_block'] = 2
        smoke['e7']['ablation_trials_per_block'] = 2
        smoke['e8']['variants'] = ['full_colocated', 'leaf_colocated', 'ext_split']
        smoke['e8']['failure_probabilities'] = [0.0, 0.1]
        smoke['e8']['outage_models'] = ['independent']
        smoke['e8']['queries_per_case'] = 20
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            cfg = run / 'config.json'
            cfg.write_text(json.dumps(smoke))
            build_plan(cfg, run)
            (run / 'raw_e7').mkdir()
            (run / 'raw_e8').mkdir()
            (run / 'results').mkdir()
            for index, _ in enumerate(read_jsonl(run / 'e7_blocks.jsonl')):
                run_e7_block(run, index, run / 'raw_e7')
            for index, _ in enumerate(read_jsonl(run / 'e8_blocks.jsonl')):
                run_e8_block(run, index, run / 'raw_e8')
            summary = analyze(run, run / 'raw_e7', run / 'raw_e8', run / 'results')
            self.assertEqual(summary['status'], 'PASS')
            ok, errors = validate_results(run / 'results')
            self.assertTrue(ok, errors)
if __name__ == '__main__':
    unittest.main()
