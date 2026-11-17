from __future__ import annotations

import unittest
from dataclasses import replace

from msi_anchor_exp.accumulator import (
    DynamicAccumulator, HashCounter, deterministic_statement, make_aggregate_evidence,
    make_checkpoint_anchor, verify_aggregate_evidence,
)
from msi_anchor_exp.crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from msi_anchor_exp.pruning import AggregateCheckpointState, simulate_pruning


def flipped(value: bytes) -> bytes:
    return value[:-1] + bytes([value[-1] ^ 1])


class CurrentCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.signer = Ed25519Signer(deterministic_seed('current-checkpoint', 1))
        self.verifier = Ed25519Verifier(self.signer.public_key)
        self.other_signer = Ed25519Signer(deterministic_seed('current-checkpoint', 2))
        self.acc = DynamicAccumulator()
        self.statements = [deterministic_statement(19, 3, i) for i in range(1, 5)]
        for statement in self.statements:
            self.acc.append_statement(statement)
        self.old = make_checkpoint_anchor(3, 2, self.acc.root(2), self.signer)
        self.current = make_checkpoint_anchor(3, 4, self.acc.root(4), self.signer)
        self.state = AggregateCheckpointState(3, self.signer.public_key)
        self.assertTrue(self.state.install(self.current, self.verifier))
        self.query = self.statements[0]
        self.evidence = make_aggregate_evidence(self.query, self.acc.proof(1, 4))
        self.old_evidence = make_aggregate_evidence(self.query, self.acc.proof(1, 2))

    def tearDown(self):
        self.signer.close()
        self.verifier.close()
        self.other_signer.close()

    def assert_rejected_without_state_change(self, evidence, candidate, expected=None):
        before = self.state.current
        self.assertFalse(self.state.verify(expected or self.query, evidence, candidate,
                                           self.verifier, HashCounter()))
        self.assertIs(self.state.current, before)

    def test_current_valid_proof_accepts_without_changing_checkpoint(self):
        before = self.state.current
        self.assertTrue(self.state.verify(self.query, self.evidence, self.current,
                                          self.verifier, HashCounter()))
        self.assertIs(self.state.current, before)

    def test_authentic_old_checkpoint_still_covering_query_is_rejected(self):
        # The old proof is authentic and covers position 1; it is not malformed.
        self.assertTrue(verify_aggregate_evidence(self.query, self.old_evidence,
                                                  self.old, self.verifier))
        self.assert_rejected_without_state_change(self.old_evidence, self.old)
        self.assert_rejected_without_state_change(self.old_evidence, self.current)
        self.assertFalse(self.state.install(self.old, self.verifier))
        self.assertEqual(self.state.current, self.current)

    def test_noncovering_checkpoint_is_rejected(self):
        query = self.statements[2]
        evidence = make_aggregate_evidence(query, self.acc.proof(3, 4))
        self.assert_rejected_without_state_change(evidence, self.old, query)

    def test_wrong_statement_position_or_root_is_rejected(self):
        wrong_statement = replace(self.query, epoch=99)
        wrong_position = self.statements[1]
        wrong_root = replace(self.query, root=flipped(self.query.root))
        for expected in (wrong_statement, wrong_position, wrong_root):
            with self.subTest(expected=expected):
                self.assert_rejected_without_state_change(self.evidence, self.current, expected)

    def test_wrong_anchor_root_key_shard_or_signature_is_rejected(self):
        candidates = (
            replace(self.current, accumulator_root=flipped(self.current.accumulator_root)),
            replace(self.current, public_key=self.other_signer.public_key),
            replace(self.current, shard=4),
            replace(self.current, signature=flipped(self.current.signature)),
            make_checkpoint_anchor(3, 4, self.current.accumulator_root, self.other_signer),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assert_rejected_without_state_change(self.evidence, candidate)
                self.assertFalse(self.state.install(candidate, self.verifier))
                self.assertEqual(self.state.current, self.current)

    def test_authentic_same_height_fork_cannot_replace_checkpoint(self):
        fork = make_checkpoint_anchor(3, 4, flipped(self.current.accumulator_root), self.signer)
        self.assertFalse(self.state.install(fork, self.verifier))
        self.assertEqual(self.state.current, self.current)
        self.assert_rejected_without_state_change(self.evidence, fork)

    def test_missing_checkpoint_rejects_proof_without_installing_it(self):
        empty = AggregateCheckpointState(3, self.signer.public_key)
        self.assertFalse(empty.verify(self.query, self.evidence, self.current, self.verifier))
        self.assertIsNone(empty.current)

    def test_authenticated_checkpoint_can_advance_and_delayed_delivery_cannot_rollback(self):
        state = AggregateCheckpointState(3, self.signer.public_key)
        self.assertTrue(state.install(self.old, self.verifier))
        self.assertTrue(state.install(self.current, self.verifier))
        self.assertFalse(state.install(self.old, self.verifier))
        self.assertEqual(state.current, self.current)

    def test_rejected_checkpoint_evidence_does_not_prune_before_valid_recovery(self):
        case = {'case_id': 'active-checkpoint-regression', 'config_id': 'regression',
                'trial': 0, 'trial_kind': 'pruning', 'policy': 'safe',
                'anchor_mode': 'aggregate', 'checkpoint_interval': 4, 'seed': 1234,
                'shard': 3, 'anchor_key_seed': 5052026, 'total_epochs': 4,
                'blocks_per_epoch': 4, 'block_size': 128, 'arrival_rate_eps': 100,
                'finality_delay_ms': 10.0, 'certificate_delay_ms': 20.0,
                'retry_delay_ms': 1000.0, 'hot_window_epochs': 16,
                'fault': 'uncovered_prefix', 'fault_fraction': 1.0,
                'timeline_sample_limit': 100}
        # First delivery: protected prefix 4, candidate prefix 1. Epoch 1 is
        # covered by both, so the old caller pruned it immediately at 60 ms.
        # Recovery supplies current evidence at 1060 ms.
        result = simulate_pruning(case)
        prune_times = [row['time_ms'] for row in result['timeline'] if row['reason_code'] == 6]
        self.assertEqual(len(prune_times), 4)
        self.assertTrue(all(t >= 1060.0 for t in prune_times), prune_times)
        self.assertEqual(result['safety']['total_gate_violations'], 0)
        self.assertEqual(result['counts']['pending_epochs'], 0)
        self.assertEqual(result['counts']['anchor_rejects'], 4)


if __name__ == '__main__':
    unittest.main()
