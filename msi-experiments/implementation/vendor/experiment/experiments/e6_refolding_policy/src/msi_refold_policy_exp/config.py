from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable
from .dataset import DatasetSpec, make_dataset_spec
from .migration import TRANSITIONS, applicable_faults
from .util import atomic_write_json, read_json, stable_seed, write_jsonl
PROFILE_WEIGHTS = {'balanced': {'alpha': 1.0, 'beta': 1.0, 'gamma': 1.0, 'delta': 1.0}, 'storage': {'alpha': 4.0, 'beta': 1.0, 'gamma': 1.0, 'delta': 1.0}, 'bandwidth': {'alpha': 1.0, 'beta': 4.0, 'gamma': 1.0, 'delta': 1.0}, 'cpu': {'alpha': 1.0, 'beta': 1.0, 'gamma': 4.0, 'delta': 1.0}, 'availability': {'alpha': 1.0, 'beta': 1.0, 'gamma': 1.0, 'delta': 4.0}}

def _case_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return f'{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}'

def _dataset_spec(*, seed: int, n: int, block_bytes: int, layout: str, codec: str) -> DatasetSpec:
    return make_dataset_spec(shard=3, epoch=17, n=int(n), block_bytes=int(block_bytes), layout=str(layout), codec=str(codec), payload_seed=stable_seed(seed, 'payload', n, block_bytes, layout, codec), anchor_key_seed=stable_seed(seed, 'anchor', n, block_bytes, layout, codec))

def _normalise_variants(config: dict[str, Any]) -> list[dict[str, Any]]:
    variants = [{'label': 'canonical', 'layout': str(config['datasets']['default_layout']), 'codec': str(config['datasets']['default_codec']), 'block_bytes': int(config['datasets']['default_block_bytes'])}]
    for row in config['migration'].get('compatibility_variants', []):
        variants.append({'label': str(row['label']), 'layout': str(row['layout']), 'codec': str(row['codec']), 'block_bytes': int(row.get('block_bytes', config['datasets']['default_block_bytes']))})
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in variants:
        unique[row['layout'], row['codec'], row['block_bytes']] = row
    return list(unique.values())

def _migration_cases(config: dict[str, Any]) -> tuple[list[DatasetSpec], list[dict[str, Any]]]:
    seed = int(config['seed'])
    migration = config['migration']
    datasets: dict[str, DatasetSpec] = {}
    cases: list[dict[str, Any]] = []
    canonical = {'label': 'canonical', 'layout': str(config['datasets']['default_layout']), 'codec': str(config['datasets']['default_codec']), 'block_bytes': int(config['datasets']['default_block_bytes'])}

    def add_case(*, trial_kind: str, n: int, transition: str, backend: str, launch: int, variant: dict[str, Any], fault: str='none') -> None:
        spec = _dataset_spec(seed=seed, n=n, block_bytes=variant['block_bytes'], layout=variant['layout'], codec=variant['codec'])
        datasets[spec.dataset_id] = spec
        payload = {'schema_version': 1, 'experiment': 'E6', 'trial_kind': trial_kind, 'dataset_id': spec.dataset_id, 'n': int(n), 'n_prime': 1 << (int(n) - 1).bit_length(), 'transition': transition, 'source_mode': transition.split('->', 1)[0], 'target_mode': transition.split('->', 1)[1], 'backend': backend, 'launch': int(launch), 'variant_label': variant['label'], 'layout': variant['layout'], 'codec': variant['codec'], 'block_bytes': int(variant['block_bytes']), 'fault': fault, 'seed': stable_seed(seed, trial_kind, n, transition, backend, launch, variant['label'], fault), 'exact_sample_limit': int(migration.get('exact_sample_limit', 64)), 'crash_timeout_seconds': float(migration.get('crash_timeout_seconds', 90.0))}
        payload['case_id'] = _case_id('e6', payload)
        cases.append(payload)
    for n in migration['performance_n_values']:
        for transition in TRANSITIONS:
            for backend in migration['backends']:
                for launch in range(int(migration['performance_launches'])):
                    add_case(trial_kind='migration_perf', n=int(n), transition=transition, backend=str(backend), launch=launch, variant=canonical)
    compatibility_variants = _normalise_variants(config)[1:]
    for n in migration.get('compatibility_n_values', []):
        for variant in compatibility_variants:
            for transition in TRANSITIONS:
                for backend in migration['backends']:
                    for launch in range(int(migration.get('compatibility_launches', 1))):
                        add_case(trial_kind='migration_perf', n=int(n), transition=transition, backend=str(backend), launch=launch, variant=variant)
    audit_variants = _normalise_variants(config) if migration.get('audit_all_variants', False) else [canonical]
    for n in migration['equivalence_n_values']:
        for variant in audit_variants:
            for transition in TRANSITIONS:
                add_case(trial_kind='equivalence_audit', n=int(n), transition=transition, backend=str(migration.get('audit_backend', migration['backends'][0])), launch=0, variant=variant)
    for n in migration['fault_n_values']:
        for transition in TRANSITIONS:
            for fault in applicable_faults(transition):
                add_case(trial_kind='fault_injection', n=int(n), transition=transition, backend=str(migration.get('fault_backend', migration['backends'][0])), launch=0, variant=canonical, fault=fault)
    seen: set[str] = set()
    for case in cases:
        if case['case_id'] in seen:
            raise ValueError(f"duplicate migration case ID: {case['case_id']}")
        seen.add(case['case_id'])
    return (sorted(datasets.values(), key=lambda value: value.dataset_id), cases)

def _policy_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    policy = config['policy']
    seed = int(config['seed'])
    profiles = policy.get('profiles', list(PROFILE_WEIGHTS))
    cases: list[dict[str, Any]] = []
    scales = policy.get('scales')
    if scales is None:
        scales = [{'label': 'primary', 'epoch_count': int(policy['epoch_count']), 'shard_count': int(policy['shard_count']), 'budgets': policy['budget_fractions'], 'workloads': policy['workloads'], 'prediction_errors': policy['prediction_errors'], 'run_dp': True}]
    for scale in scales:
        for profile_name in profiles:
            profile = PROFILE_WEIGHTS.get(str(profile_name))
            if profile is None:
                raise ValueError(f'unknown policy profile: {profile_name}')
            for budget in scale['budgets']:
                for workload in scale['workloads']:
                    for error in scale['prediction_errors']:
                        payload = {'schema_version': 1, 'experiment': 'E9', 'trial_kind': 'policy', 'scale_label': str(scale['label']), 'epoch_count': int(scale['epoch_count']), 'shard_count': int(scale['shard_count']), 'n_values': [int(value) for value in policy['n_values']], 'profile': str(profile_name), 'profile_weights': dict(profile), 'budget_fraction': float(budget), 'workload': str(workload), 'prediction_error': float(error), 'trace_replicates': int(scale.get('trace_replicates', policy['trace_replicates'])), 'evaluation_queries': int(scale.get('evaluation_queries', policy['evaluation_queries'])), 'storage_quantum_bytes': int(policy.get('storage_quantum_bytes', 4096)), 'run_dp': bool(scale.get('run_dp', True)), 'seed': stable_seed(seed, 'policy', scale['label'], profile_name, budget, workload, error)}
                        payload['case_id'] = _case_id('e9', payload)
                        cases.append(payload)
    return cases

def _blocks(cases: list[dict[str, Any]], block_size: int, prefix: str) -> list[dict[str, Any]]:
    if block_size < 1:
        raise ValueError('block size must be positive')
    rows = []
    for block_index, start in enumerate(range(0, len(cases), block_size)):
        selected = cases[start:start + block_size]
        rows.append({'schema_version': 1, 'block_id': f'{prefix}-{block_index:05d}', 'block_index': block_index, 'case_indices': list(range(start, start + len(selected))), 'case_ids': [case['case_id'] for case in selected]})
    return rows

def make_plans(config_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    config_path = Path(config_path)
    config = read_json(config_path)
    if int(config.get('schema_version', 0)) != 1:
        raise ValueError('unsupported configuration schema')
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    datasets, migration_cases = _migration_cases(config)
    policy_cases = _policy_cases(config)
    migration_blocks = _blocks(migration_cases, int(config['migration'].get('block_size', 4)), 'migration-block')
    policy_blocks = _blocks(policy_cases, int(config['policy'].get('block_size', 4)), 'policy-block')
    write_jsonl(output / 'dataset_plan.jsonl', [value.as_dict() for value in datasets])
    write_jsonl(output / 'migration_plan.jsonl', migration_cases)
    write_jsonl(output / 'migration_blocks.jsonl', migration_blocks)
    write_jsonl(output / 'policy_plan.jsonl', policy_cases)
    write_jsonl(output / 'policy_blocks.jsonl', policy_blocks)
    kinds: dict[str, int] = {}
    for case in migration_cases:
        kinds[case['trial_kind']] = kinds.get(case['trial_kind'], 0) + 1
    transitions = {transition: sum((1 for case in migration_cases if case['transition'] == transition)) for transition in TRANSITIONS}
    summary = {'schema_version': 1, 'experiment': 'E6+E9', 'config_path': str(config_path.resolve()), 'label': config.get('label', config_path.stem), 'datasets': len(datasets), 'migration_cases': len(migration_cases), 'migration_blocks': len(migration_blocks), 'migration_case_kinds': kinds, 'transition_case_counts': transitions, 'policy_cases': len(policy_cases), 'policy_blocks': len(policy_blocks), 'policy_replicates': sum((int(case['trace_replicates']) for case in policy_cases)), 'policy_algorithm_rows': sum((int(case['trace_replicates']) * (7 if case['run_dp'] else 6) for case in policy_cases)), 'component_catalog_requirement': str(config['policy'].get('component_catalog_requirement', 'external-prior-results-for-publication')), 'publication_mode': bool(config.get('publication_mode', False))}
    atomic_write_json(output / 'plan_summary.json', summary)
    atomic_write_json(output / 'resolved_config.json', config)
    return summary

def count_plan(config_path: str | Path) -> dict[str, Any]:
    import tempfile
    with tempfile.TemporaryDirectory(prefix='e6-plan-') as directory:
        return make_plans(config_path, directory)
