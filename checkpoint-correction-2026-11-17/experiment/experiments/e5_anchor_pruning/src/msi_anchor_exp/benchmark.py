from __future__ import annotations
import gc
import hashlib
import json
import math
import os
import resource
import time
from pathlib import Path
from typing import Any
from .accumulator import AggregateAnchor, DirectAnchor, HashCounter, aggregate_anchor_bytes, aggregate_evidence_bytes_for_power_of_two, checkpoint_object_bytes, direct_anchor_bytes, direct_evidence_bytes, make_aggregate_evidence, make_checkpoint_anchor, make_direct_evidence, verify_aggregate_evidence, verify_direct_evidence
from .crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from .dataset import DatasetSpec, MappedAccumulatorDataset, dataset_path, validate_dataset
from .pruning import simulate_pruning
from .util import atomic_write_json_gz, pin_to_first_available_cpu, stable_seed, summarise, system_metadata

def _ru_maxrss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value * 1024 if os.uname().sysname == 'Linux' else value

def _positions(prefix_count: int, count: int, seed: int) -> list[int]:
    if count <= 0:
        return []
    step = (stable_seed(seed, 'position-step') | 1) % prefix_count
    if step == 0:
        step = 1
    while math.gcd(step, prefix_count) != 1:
        step = (step + 2) % prefix_count or 1
    offset = stable_seed(seed, 'position-offset') % prefix_count
    return [(offset + index * step) % prefix_count + 1 for index in range(count)]

def _query_order_digest(positions: list[int]) -> str:
    digest = hashlib.sha256()
    for value in positions:
        digest.update(value.to_bytes(8, 'big'))
    return digest.hexdigest()

def _anchor_query(case: dict[str, Any], dataset_dir: Path) -> dict[str, Any]:
    spec = DatasetSpec.from_dict(case)
    validation = validate_dataset(dataset_path(dataset_dir, spec), spec, verify_hashes=False)
    if validation['status'] != 'PASS':
        raise RuntimeError(f"dataset validation failed: {validation['errors']}")
    mode = str(case['anchor_mode'])
    prefix_count = int(case['prefix_count'])
    warmup = int(case['warmup_queries'])
    measured = int(case['measured_queries'])
    seed = int(case['seed'])
    positions = _positions(prefix_count, warmup + measured, seed)
    pinning = {'attempted': False, 'success': None} if bool(case.get('disable_pinning', False)) else pin_to_first_available_cpu()
    signer = Ed25519Signer(deterministic_seed('checkpoint', int(case['anchor_key_seed'])))
    verifier = Ed25519Verifier(signer.public_key)
    generation_samples: list[int] = []
    verification_samples: list[int] = []
    evidence_sizes: list[int] = []
    proof_hash_counts: list[int] = []
    all_accepted = True
    tamper_rejected = False
    process_cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    rss_before = _ru_maxrss_bytes()
    try:
        with MappedAccumulatorDataset(dataset_path(dataset_dir, spec)) as dataset:
            if prefix_count > dataset.spec.max_prefix:
                raise ValueError('query prefix exceeds prepared dataset')
            if mode == 'direct':
                anchor: DirectAnchor | AggregateAnchor = DirectAnchor(shard=spec.shard, public_key=signer.public_key)
            elif mode == 'aggregate':
                anchor = make_checkpoint_anchor(spec.shard, prefix_count, dataset.power_of_two_root(prefix_count), signer)
            else:
                raise ValueError(f'unsupported anchor mode: {mode}')

            def one(position: int, measured_phase: bool) -> tuple[bytes, bool, int]:
                statement = dataset.statement(position)
                started = time.perf_counter_ns()
                if mode == 'direct':
                    evidence = make_direct_evidence(statement, signer)
                else:
                    proof = dataset.proof_power_of_two(position, prefix_count)
                    evidence = make_aggregate_evidence(statement, proof)
                generation_elapsed = time.perf_counter_ns() - started
                counter = HashCounter()
                started = time.perf_counter_ns()
                if mode == 'direct':
                    accepted = verify_direct_evidence(statement, evidence, anchor, verifier)
                else:
                    accepted = verify_aggregate_evidence(statement, evidence, anchor, verifier, counter)
                verification_elapsed = time.perf_counter_ns() - started
                if measured_phase:
                    generation_samples.append(generation_elapsed)
                    verification_samples.append(verification_elapsed)
                    evidence_sizes.append(len(evidence))
                    proof_hash_counts.append(counter.total)
                return (evidence, accepted, counter.total)
            gc_enabled = gc.isenabled()
            gc.disable()
            try:
                for position in positions[:warmup]:
                    _, accepted, _ = one(position, False)
                    all_accepted = all_accepted and accepted
                first_evidence: bytes | None = None
                first_statement = None
                for position in positions[warmup:]:
                    evidence, accepted, _ = one(position, True)
                    if first_evidence is None:
                        first_evidence = evidence
                        first_statement = dataset.statement(position)
                    all_accepted = all_accepted and accepted
            finally:
                if gc_enabled:
                    gc.enable()
            if first_evidence is not None and first_statement is not None:
                corrupted = first_evidence[:-1] + bytes([first_evidence[-1] ^ 1])
                if mode == 'direct':
                    tamper_rejected = not verify_direct_evidence(first_statement, corrupted, anchor, verifier)
                else:
                    tamper_rejected = not verify_aggregate_evidence(first_statement, corrupted, anchor, verifier, HashCounter())
    finally:
        signer.close()
        verifier.close()
    wall_elapsed = time.perf_counter_ns() - wall_start
    process_cpu_elapsed = time.process_time_ns() - process_cpu_start
    rss_after = _ru_maxrss_bytes()
    expected_evidence = direct_evidence_bytes() if mode == 'direct' else aggregate_evidence_bytes_for_power_of_two(prefix_count)
    expected_hashes = 0 if mode == 'direct' else prefix_count.bit_length() - 1 + 2
    checkpoint_interval = int(case.get('accounting_checkpoint_interval', 100))
    external_certificate_bytes = prefix_count * direct_evidence_bytes() if mode == 'direct' else math.ceil(prefix_count / checkpoint_interval) * checkpoint_object_bytes()
    result = {'schema_version': 1, 'experiment': 'E5', 'trial_kind': 'anchor_query', 'case': case, 'dataset_validation': validation, 'pinning': pinning, 'host': system_metadata(), 'query_order_sha256': _query_order_digest(positions), 'counts': {'warmup_queries': warmup, 'measured_queries': measured, 'accepted_queries': measured if all_accepted else sum((1 for _ in verification_samples)), 'signature_generation_operations_per_query': 1 if mode == 'direct' else 0, 'signature_verification_operations_per_query': 1, 'sha256_operations_per_query': expected_hashes}, 'storage': {'verifier_resident_anchor_bytes': direct_anchor_bytes() if mode == 'direct' else aggregate_anchor_bytes(), 'evidence_bytes': summarise(evidence_sizes), 'expected_evidence_bytes': expected_evidence, 'accounting_checkpoint_interval': checkpoint_interval, 'external_certificate_bytes_at_prefix': external_certificate_bytes}, 'latency': {'evidence_generation_ns': summarise(generation_samples), 'verify_anchor_ns': summarise(verification_samples)}, 'resources': {'wall_ns': wall_elapsed, 'process_cpu_ns': process_cpu_elapsed, 'cpu_utilisation_ratio': process_cpu_elapsed / wall_elapsed if wall_elapsed else None, 'ru_maxrss_before_bytes': rss_before, 'ru_maxrss_after_bytes': rss_after}, 'samples': {'evidence_generation_ns': generation_samples, 'verify_anchor_ns': verification_samples}}
    result['checks'] = {'all_valid_evidence_accepted': all_accepted, 'single_bit_tamper_rejected': tamper_rejected, 'evidence_size_exact': set(evidence_sizes) == {expected_evidence}, 'proof_hash_count_exact': set(proof_hash_counts) == {expected_hashes}, 'sample_count_exact': len(verification_samples) == measured}
    result['checks']['all_internal_checks_pass'] = all(result['checks'].values())
    return result

def _checkpoint_update(case: dict[str, Any], dataset_dir: Path) -> dict[str, Any]:
    spec = DatasetSpec.from_dict(case)
    validation = validate_dataset(dataset_path(dataset_dir, spec), spec, verify_hashes=False)
    if validation['status'] != 'PASS':
        raise RuntimeError(f"dataset validation failed: {validation['errors']}")
    mode = str(case['anchor_mode'])
    target_prefix = int(case['target_prefix'])
    checkpoint_interval = int(case['checkpoint_interval'])
    warmup = int(case['warmup_updates'])
    repetitions = int(case['measured_updates'])
    if target_prefix < checkpoint_interval:
        raise ValueError('target_prefix is smaller than the checkpoint interval')
    pinning = {'attempted': False, 'success': None} if bool(case.get('disable_pinning', False)) else pin_to_first_available_cpu()
    signer = Ed25519Signer(deterministic_seed('checkpoint', int(case['anchor_key_seed'])))
    update_samples: list[int] = []
    amortized_samples: list[float] = []
    hash_counts: list[int] = []
    root_matches: list[bool] = []
    signature_lengths: list[int] = []
    process_cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    rss_before = _ru_maxrss_bytes()
    try:
        with MappedAccumulatorDataset(dataset_path(dataset_dir, spec)) as dataset:
            expected_root = dataset.power_of_two_root(target_prefix)
            if mode == 'aggregate':
                snapshot = dataset.frontier_at(target_prefix - checkpoint_interval)
                statements = list(dataset.iter_statements(target_prefix - checkpoint_interval + 1, target_prefix))
            elif mode == 'direct':
                snapshot = None
                statements = [dataset.statement(target_prefix)]
            else:
                raise ValueError(f'unsupported anchor mode: {mode}')

            def one(measured_phase: bool, iteration: int) -> None:
                if mode == 'direct':
                    position = (target_prefix - 1 + iteration) % target_prefix + 1
                    statement = dataset.statement(position)
                    started = time.perf_counter_ns()
                    evidence = make_direct_evidence(statement, signer)
                    elapsed = time.perf_counter_ns() - started
                    counter = HashCounter()
                    root_ok = True
                    signature_length = len(evidence) - direct_evidence_bytes() + 64
                    denominator = 1
                else:
                    frontier = snapshot.clone()
                    counter = HashCounter()
                    started = time.perf_counter_ns()
                    for statement in statements:
                        frontier.append_statement(statement, counter)
                    root = frontier.root(counter)
                    anchor = make_checkpoint_anchor(spec.shard, target_prefix, root, signer)
                    elapsed = time.perf_counter_ns() - started
                    root_ok = root == expected_root
                    signature_length = len(anchor.signature)
                    denominator = checkpoint_interval
                if measured_phase:
                    update_samples.append(elapsed)
                    amortized_samples.append(elapsed / denominator)
                    hash_counts.append(counter.total)
                    root_matches.append(root_ok)
                    signature_lengths.append(signature_length)
            gc_enabled = gc.isenabled()
            gc.disable()
            try:
                for iteration in range(warmup):
                    one(False, iteration)
                for iteration in range(repetitions):
                    one(True, iteration)
            finally:
                if gc_enabled:
                    gc.enable()
    finally:
        signer.close()
    wall_elapsed = time.perf_counter_ns() - wall_start
    process_cpu_elapsed = time.process_time_ns() - process_cpu_start
    rss_after = _ru_maxrss_bytes()
    checkpoints_at_prefix = target_prefix if mode == 'direct' else math.ceil(target_prefix / checkpoint_interval)
    external_certificate_bytes = target_prefix * direct_evidence_bytes() if mode == 'direct' else checkpoints_at_prefix * checkpoint_object_bytes()
    result = {'schema_version': 1, 'experiment': 'E5', 'trial_kind': 'checkpoint_update', 'case': case, 'dataset_validation': validation, 'pinning': pinning, 'host': system_metadata(), 'counts': {'warmup_updates': warmup, 'measured_updates': repetitions, 'statements_per_update': 1 if mode == 'direct' else checkpoint_interval, 'signature_operations_per_update': 1, 'checkpoint_objects_at_target_prefix': checkpoints_at_prefix}, 'storage': {'verifier_resident_anchor_bytes': direct_anchor_bytes() if mode == 'direct' else aggregate_anchor_bytes(), 'external_certificate_bytes_at_prefix': external_certificate_bytes, 'checkpoint_object_bytes': direct_evidence_bytes() if mode == 'direct' else checkpoint_object_bytes()}, 'latency': {'checkpoint_update_ns': summarise(update_samples), 'amortized_update_ns_per_epoch': summarise(amortized_samples)}, 'crypto': {'sha256_operations_per_update': summarise(hash_counts), 'signature_bytes': summarise(signature_lengths)}, 'resources': {'wall_ns': wall_elapsed, 'process_cpu_ns': process_cpu_elapsed, 'cpu_utilisation_ratio': process_cpu_elapsed / wall_elapsed if wall_elapsed else None, 'ru_maxrss_before_bytes': rss_before, 'ru_maxrss_after_bytes': rss_after}, 'samples': {'checkpoint_update_ns': update_samples, 'amortized_update_ns_per_epoch': amortized_samples}}
    result['checks'] = {'sample_count_exact': len(update_samples) == repetitions, 'all_target_roots_match': all(root_matches), 'signature_length_exact': set(signature_lengths) == {64}, 'hash_count_stable': len(set(hash_counts)) <= 1}
    result['checks']['all_internal_checks_pass'] = all(result['checks'].values())
    return result

def case_result_path(raw_dir: str | Path, case_id: str) -> Path:
    return Path(raw_dir) / f'{case_id}.json.gz'

def run_case(case: dict[str, Any], dataset_dir: str | Path, raw_dir: str | Path) -> dict[str, Any]:
    started = time.time()
    trial_kind = str(case['trial_kind'])
    if trial_kind == 'anchor_query':
        result = _anchor_query(case, Path(dataset_dir))
    elif trial_kind == 'checkpoint_update':
        result = _checkpoint_update(case, Path(dataset_dir))
    elif trial_kind in {'pruning', 'unsafe_counterexample'}:
        result = simulate_pruning(case)
        result['host'] = system_metadata()
    else:
        raise ValueError(f'unsupported trial_kind: {trial_kind}')
    result['artifact_runtime'] = {'started_unix_seconds': started, 'finished_unix_seconds': time.time()}
    output = case_result_path(raw_dir, str(case['case_id']))
    atomic_write_json_gz(output, result)
    return result
