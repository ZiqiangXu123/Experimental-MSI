from __future__ import annotations
import csv
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from msi_refold_policy_exp.catalog import load_cost_catalog, validate_cost_catalog, write_reference_catalog
from msi_refold_policy_exp.config import count_plan
from msi_refold_policy_exp.crypto import openssl_info
from msi_refold_policy_exp.dataset import build_dataset, make_dataset_spec, validate_dataset
from msi_refold_policy_exp.merkle import HashCounter, build_layers, canonical_block, leaf_hash, next_power_of_two, path_from_layers, rebuild_path_from_leaf_vector, verify_path
from msi_refold_policy_exp.migration import TRANSITIONS, applicable_faults, run_migration_case
from msi_refold_policy_exp.policy import assignment_objective, exact_multiple_choice_dp, generate_workload_windows, make_epoch_items, normalisation

class Experiment6Tests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='e6-test-')
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _dataset(self, n: int=9) -> tuple[Path, object]:
        dataset_dir = self.root / f'datasets-{n}'
        spec = make_dataset_spec(shard=2, epoch=7, n=n, block_bytes=256, layout='epoch_packed', codec='raw-v1', payload_seed=1234 + n, anchor_key_seed=5678 + n)
        build_dataset(dataset_dir, spec)
        return (dataset_dir, spec)

    def _migration_case(self, spec: object, transition: str, kind: str, fault: str='none') -> dict:
        return {'case_id': f'test-{kind}-{transition}-{fault}'.replace('->', '-'), 'dataset_id': spec.dataset_id, 'transition': transition, 'backend': 'node_local', 'trial_kind': kind, 'n': spec.n, 'n_prime': spec.n_prime, 'seed': 11, 'fault': fault, 'exact_sample_limit': 64, 'crash_timeout_seconds': 30}

    def test_01_openssl_ed25519(self) -> None:
        self.assertTrue(openssl_info()['available'])

    def test_02_duplicate_last_merkle_and_algorithm_s1(self) -> None:
        n = 9
        counter = HashCounter()
        leaves = [leaf_hash(1, 2, i, canonical_block(shard=1, epoch=2, position=i, block_bytes=128, seed=9), counter) for i in range(n)]
        leaves.extend([leaves[-1]] * (next_power_of_two(n) - n))
        layers = build_layers(leaves, counter)
        for position in (1, 2, 9):
            direct = path_from_layers(layers, position)
            rebuilt = rebuild_path_from_leaf_vector(leaves, position, HashCounter())
            self.assertEqual(direct, rebuilt)
            block = canonical_block(shard=1, epoch=2, position=position - 1, block_bytes=128, seed=9)
            self.assertTrue(verify_path(shard=1, epoch=2, position=position - 1, block=block, path=direct, expected_root=layers[-1][0], counter=HashCounter()))

    def test_03_dataset_manifest_and_all_modes(self) -> None:
        dataset_dir, spec = self._dataset(65)
        audit = validate_dataset(dataset_dir / spec.dataset_id, spec, verify_hashes=True)
        self.assertEqual(audit['status'], 'PASS')
        self.assertEqual(audit['n_prime'], 128)

    def test_04_all_six_transitions_preserve_state(self) -> None:
        dataset_dir, spec = self._dataset(9)
        for transition in TRANSITIONS:
            result = run_migration_case(self._migration_case(spec, transition, 'equivalence_audit'), dataset_dir=dataset_dir, work_root=self.root / 'work')
            self.assertTrue(result['all_checks_pass'], transition)
            self.assertTrue(result['accepted_relation_equivalent'], transition)

    def test_05_complete_fault_matrix_fails_closed(self) -> None:
        dataset_dir, spec = self._dataset(8)
        observed = 0
        for transition in TRANSITIONS:
            for fault in applicable_faults(transition):
                result = run_migration_case(self._migration_case(spec, transition, 'fault_injection', fault), dataset_dir=dataset_dir, work_root=self.root / 'fault-work')
                self.assertTrue(result['all_checks_pass'], (transition, fault))
                observed += 1
        self.assertEqual(observed, 46)

    def _policy_fixture(self):
        component = self.root / 'components.csv'
        write_reference_catalog(component, [8, 64])
        catalog = load_cost_catalog(component)
        migration = self.root / 'migration.csv'
        fields = ['n', 'transition', 'wall_ns_median', 'bytes_read_median', 'bytes_written_median']
        with migration.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for n in (8, 64):
                for source in ('full', 'leaf', 'ext'):
                    for target in ('full', 'leaf', 'ext'):
                        if source != target:
                            writer.writerow({'n': n, 'transition': f'{source}->{target}', 'wall_ns_median': 100000 + n * 100, 'bytes_read_median': 1000 + n, 'bytes_written_median': 2000 + n})
        return (component, catalog, migration)

    def test_06_exact_dp_matches_bruteforce(self) -> None:
        _component, catalog, _migration = self._policy_fixture()
        items = make_epoch_items(epoch_count=5, shard_count=1, n_values=[8, 64], catalog=catalog, seed=99)
        weights = [0.4, 0.25, 0.15, 0.1, 0.1]
        profile = {'alpha': 1.0, 'beta': 1.0, 'gamma': 1.0, 'delta': 1.0}
        norms = normalisation(items)
        budget = int(0.5 * norms['all_full_storage_bytes'] // 4096 * 4096)
        assignment, value, metadata = exact_multiple_choice_dp(items=items, weights=weights, profile=profile, norms=norms, budget_bytes=budget, storage_quantum_bytes=4096)
        brute = []
        for candidate in itertools.product(('full', 'leaf', 'ext'), repeat=len(items)):
            storage = sum((item.costs[mode].storage_bytes for item, mode in zip(items, candidate)))
            if storage <= budget:
                brute.append((assignment_objective(items, candidate, weights, profile, norms), candidate))
        best = min(brute, key=lambda row: row[0])
        self.assertAlmostEqual(value, best[0], places=12)
        self.assertAlmostEqual(assignment_objective(items, assignment, weights, profile, norms), best[0], places=12)
        self.assertTrue(metadata['exact_for_aligned_storage_costs'])

    def test_07_prediction_error_model(self) -> None:
        _, exact, actual = generate_workload_windows(count=64, workload='zipf1.2', prediction_error=0.0, seed=1)
        self.assertEqual(exact, actual)
        _, noisy, actual2 = generate_workload_windows(count=64, workload='zipf1.2', prediction_error=0.5, seed=1)
        self.assertNotEqual(noisy, actual2)
        self.assertAlmostEqual(sum(noisy), 1.0, places=12)

    def test_08_reference_catalog_is_explicitly_nonpublication(self) -> None:
        path = self.root / 'catalog.csv'
        write_reference_catalog(path, [64, 512])
        audit = validate_cost_catalog(load_cost_catalog(path), required_n=[64, 512])
        self.assertEqual(audit['status'], 'PASS')
        self.assertFalse(audit['all_publication_eligible'])
        strict = validate_cost_catalog(load_cost_catalog(path), required_n=[64], require_publication_eligible=True)
        self.assertEqual(strict['status'], 'FAIL')

    def test_09_publication_plan_is_fixed(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        summary = count_plan(package_root / 'configs' / 'publication.json')
        self.assertEqual(summary['datasets'], 22)
        self.assertEqual(summary['migration_cases'], 1410)
        self.assertEqual(summary['migration_blocks'], 353)
        self.assertEqual(summary['policy_cases'], 590)
        self.assertEqual(summary['policy_blocks'], 118)
        self.assertEqual(summary['policy_algorithm_rows'], 19120)

    def test_10_padding_boundary_dataset_ids_are_distinct(self) -> None:
        specs = [make_dataset_spec(shard=1, epoch=1, n=n, block_bytes=256, layout='epoch_packed', codec='raw-v1', payload_seed=1, anchor_key_seed=2) for n in (63, 64, 65)]
        self.assertEqual(len({spec.dataset_id for spec in specs}), 3)
        self.assertEqual([spec.n_prime for spec in specs], [64, 64, 128])
if __name__ == '__main__':
    unittest.main()
