from __future__ import annotations
import hashlib
import json
import platform
from pathlib import Path
import msi_anchor_exp.pruning as pruning
from msi_anchor_exp.accumulator import DynamicAccumulator, deterministic_statement, make_aggregate_evidence, make_checkpoint_anchor, verify_aggregate_evidence
from msi_anchor_exp.crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed, openssl_info

case = {'case_id': 'active-checkpoint-regression', 'config_id': 'regression',
        'trial': 0, 'trial_kind': 'pruning', 'policy': 'safe',
        'anchor_mode': 'aggregate', 'checkpoint_interval': 4, 'seed': 1234,
        'shard': 3, 'anchor_key_seed': 5052026, 'total_epochs': 4,
        'blocks_per_epoch': 4, 'block_size': 128, 'arrival_rate_eps': 100,
        'finality_delay_ms': 10.0, 'certificate_delay_ms': 20.0,
        'retry_delay_ms': 1000.0, 'hot_window_epochs': 16,
        'fault': 'uncovered_prefix', 'fault_fraction': 1.0,
        'timeline_sample_limit': 100}
result = pruning.simulate_pruning(case)
prune_times = [row['time_ms'] for row in result['timeline'] if row['reason_code'] == 6]
acc = DynamicAccumulator()
statements = [deterministic_statement(19, 3, i) for i in range(1, 5)]
for statement in statements:
    acc.append_statement(statement)
with Ed25519Signer(deterministic_seed('current-checkpoint', 1)) as signer, Ed25519Verifier(signer.public_key) as verifier:
    old = make_checkpoint_anchor(3, 2, acc.root(2), signer)
    current = make_checkpoint_anchor(3, 4, acc.root(4), signer)
    old_evidence = make_aggregate_evidence(statements[0], acc.proof(1, 2))
    current_evidence = make_aggregate_evidence(statements[0], acc.proof(1, 4))
    proof_checks = {
        'query_position': 1, 'old_prefix': 2, 'protected_prefix': 4,
        'old_proof_against_own_authentic_anchor': verify_aggregate_evidence(statements[0], old_evidence, old, verifier),
        'old_proof_against_current_anchor': verify_aggregate_evidence(statements[0], old_evidence, current, verifier),
        'current_proof_against_current_anchor': verify_aggregate_evidence(statements[0], current_evidence, current, verifier),
    }
    if hasattr(pruning, 'AggregateCheckpointState'):
        state = pruning.AggregateCheckpointState(3, signer.public_key)
        state.install(current, verifier)
        proof_checks['protected_state_accepts_old_candidate'] = state.verify(statements[0], old_evidence, old, verifier)
        proof_checks['protected_state_accepts_current_candidate'] = state.verify(statements[0], current_evidence, current, verifier)
        proof_checks['rollback_install_accepted'] = state.install(old, verifier)
        proof_checks['protected_prefix_after_rejected_inputs'] = state.current.prefix_count
source = Path(pruning.__file__)
print(json.dumps({
    'purpose': 'Targeted correctness regression; not a publication benchmark rerun',
    'python': platform.python_version(), 'platform': platform.platform(), 'crypto': openssl_info(),
    'source_path': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'authentic_old_checkpoint_case': proof_checks,
    'pruning_regression': {'valid_recovery_time_ms': 1060.0, 'prune_times_ms': prune_times,
        'pruned_before_valid_recovery': any(t < 1060.0 for t in prune_times), 'result': result},
}, indent=2))
