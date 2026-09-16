from __future__ import annotations
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any
from .dataset import DatasetSpec
from .util import read_json, stable_seed, write_jsonl
IGNORED_CONFIG_KEYS = {'case_id', 'config_id', 'trial', 'seed', 'sweep_tags', 'primary_sweep'}

def _canonical_configuration(configuration: dict[str, Any]) -> str:
    return json.dumps({key: value for key, value in configuration.items() if key not in IGNORED_CONFIG_KEYS}, sort_keys=True, separators=(',', ':'))

def _merge(*values: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for value in values:
        output.update(value)
    return output

def _dataset_fields(config: dict[str, Any]) -> dict[str, Any]:
    dataset = config['dataset']
    return {'max_prefix': int(dataset['max_prefix']), 'shard': int(dataset.get('shard', 1)), 'statement_seed': int(dataset['statement_seed'])}

def _base_fields(config: dict[str, Any]) -> dict[str, Any]:
    return {**_dataset_fields(config), 'anchor_key_seed': int(config['anchor_key_seed']), 'disable_pinning': False}

def make_configurations(config: dict[str, Any]) -> list[dict[str, Any]]:
    base = _base_fields(config)
    configurations: dict[str, dict[str, Any]] = {}

    def add(tag: str, trial_count: int, values: dict[str, Any]) -> None:
        configuration = _merge(base, values)
        configuration['trial_count'] = int(trial_count)
        configuration['sweep_tags'] = [tag]
        configuration['primary_sweep'] = tag
        key = _canonical_configuration(configuration)
        if key in configurations:
            existing = configurations[key]
            if existing['trial_count'] != configuration['trial_count']:
                raise ValueError('deduplicated configurations have different trial counts')
            if tag not in existing['sweep_tags']:
                existing['sweep_tags'].append(tag)
        else:
            configurations[key] = configuration
    query = config['anchor_query']
    for mode, prefix in itertools.product(query['modes'], query['prefix_lengths']):
        add('anchor_query', int(query['launches']), {'trial_kind': 'anchor_query', 'anchor_mode': str(mode), 'prefix_count': int(prefix), 'warmup_queries': int(query['warmup_queries']), 'measured_queries': int(query['measured_queries']), 'accounting_checkpoint_interval': int(query['accounting_checkpoint_interval'])})
    update = config['checkpoint_update']
    target_operations = int(update['target_statement_operations'])
    minimum = int(update['minimum_measured_updates'])
    maximum = int(update['maximum_measured_updates'])
    for prefix in update['prefix_lengths']:
        prefix = int(prefix)
        if bool(update.get('include_direct', True)):
            add('checkpoint_update', int(update['launches']), {'trial_kind': 'checkpoint_update', 'anchor_mode': 'direct', 'target_prefix': prefix, 'checkpoint_interval': 1, 'warmup_updates': int(update['warmup_updates']), 'measured_updates': maximum})
        for interval in update['aggregate_intervals']:
            interval = int(interval)
            repetitions = max(minimum, min(maximum, math.ceil(target_operations / interval)))
            add('checkpoint_update', int(update['launches']), {'trial_kind': 'checkpoint_update', 'anchor_mode': 'aggregate', 'target_prefix': prefix, 'checkpoint_interval': interval, 'warmup_updates': int(update['warmup_updates']), 'measured_updates': repetitions})
    pruning = config['pruning']
    pruning_base = {'trial_kind': 'pruning', 'policy': 'safe', 'total_epochs': int(pruning['total_epochs']), 'blocks_per_epoch': int(pruning['blocks_per_epoch']), 'block_size': int(pruning['block_size']), 'finality_delay_ms': float(pruning['finality_delay_ms']), 'retry_delay_ms': float(pruning['retry_delay_ms']), 'fault_fraction': float(pruning['fault_fraction']), 'timeline_sample_limit': int(pruning['timeline_sample_limit']), 'hot_window_epochs': int(pruning['base_hot_window_epochs']), 'fault': 'none'}
    launches = int(pruning['launches'])
    direct = pruning['direct_delay_rate_grid']
    for rate, delay in itertools.product(direct['arrival_rates_eps'], direct['certificate_delays_ms']):
        add('prune_direct_delay_rate', launches, _merge(pruning_base, {'anchor_mode': 'direct', 'checkpoint_interval': 1, 'arrival_rate_eps': float(rate), 'certificate_delay_ms': float(delay)}))
    aggregate = pruning['aggregate_delay_interval_grid']
    for interval, delay in itertools.product(aggregate['checkpoint_intervals'], aggregate['certificate_delays_ms']):
        add('prune_aggregate_interval_delay', launches, _merge(pruning_base, {'anchor_mode': 'aggregate', 'checkpoint_interval': int(interval), 'arrival_rate_eps': float(aggregate['fixed_arrival_rate_eps']), 'certificate_delay_ms': float(delay)}))
    rate_sweep = pruning['aggregate_rate_sweep']
    for rate in rate_sweep['arrival_rates_eps']:
        add('prune_aggregate_rate', launches, _merge(pruning_base, {'anchor_mode': 'aggregate', 'checkpoint_interval': int(rate_sweep['checkpoint_interval']), 'arrival_rate_eps': float(rate), 'certificate_delay_ms': float(rate_sweep['certificate_delay_ms'])}))
    hot = pruning['hot_window_sweep']
    for window in hot['hot_window_epochs']:
        add('prune_hot_window', launches, _merge(pruning_base, {'anchor_mode': str(hot['anchor_mode']), 'checkpoint_interval': int(hot['checkpoint_interval']), 'arrival_rate_eps': float(hot['arrival_rate_eps']), 'certificate_delay_ms': float(hot['certificate_delay_ms']), 'hot_window_epochs': int(window)}))
    faults = pruning['fault_sweep']
    for mode, fault_names in (('direct', faults['direct_faults']), ('aggregate', faults['aggregate_faults'])):
        for fault in fault_names:
            add(f'prune_fault_{mode}', launches, _merge(pruning_base, {'anchor_mode': mode, 'checkpoint_interval': 1 if mode == 'direct' else int(faults['aggregate_checkpoint_interval']), 'arrival_rate_eps': float(faults['arrival_rate_eps']), 'certificate_delay_ms': float(faults['certificate_delay_ms']), 'hot_window_epochs': int(faults['hot_window_epochs']), 'fault': str(fault)}))
    unsafe = config['unsafe_counterexample']
    for mode in unsafe['anchor_modes']:
        add('unsafe_counterexample', int(unsafe['launches']), {'trial_kind': 'unsafe_counterexample', 'policy': 'unsafe_delete_after_upload', 'anchor_mode': str(mode), 'checkpoint_interval': 1 if mode == 'direct' else int(unsafe['aggregate_checkpoint_interval']), 'total_epochs': int(unsafe['total_epochs']), 'blocks_per_epoch': int(pruning['blocks_per_epoch']), 'block_size': int(pruning['block_size']), 'arrival_rate_eps': float(unsafe['arrival_rate_eps']), 'finality_delay_ms': float(pruning['finality_delay_ms']), 'certificate_delay_ms': float(unsafe['certificate_delay_ms']), 'retry_delay_ms': float(pruning['retry_delay_ms']), 'hot_window_epochs': int(unsafe['hot_window_epochs']), 'fault': 'none', 'fault_fraction': float(pruning['fault_fraction']), 'timeline_sample_limit': int(pruning['timeline_sample_limit'])})
    output = list(configurations.values())
    for configuration in output:
        configuration['sweep_tags'] = sorted(configuration['sweep_tags'])
        configuration['primary_sweep'] = configuration['sweep_tags'][0]
    return sorted(output, key=_canonical_configuration)

def make_plan(config_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    config = read_json(config_path)
    configurations = make_configurations(config)
    plan_seed = int(config['plan_seed'])
    rows: list[dict[str, Any]] = []
    for configuration in configurations:
        material = _canonical_configuration(configuration)
        config_id = hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]
        trial_count = int(configuration['trial_count'])
        for trial in range(trial_count):
            row = dict(configuration)
            row.pop('trial_count', None)
            row.update({'schema_version': 1, 'experiment': 'E5', 'config_id': config_id, 'trial': trial, 'seed': stable_seed(plan_seed, config_id, trial)})
            row['case_id'] = f"{row['primary_sweep']}-{config_id}-t{trial:02d}"
            rows.append(row)
    rows.sort(key=lambda row: row['case_id'])
    write_jsonl(output_path, rows)
    dataset = DatasetSpec.from_dict(_dataset_fields(config))
    counts_by_kind: dict[str, int] = {}
    configurations_by_kind: dict[str, int] = {}
    for row in rows:
        counts_by_kind[row['trial_kind']] = counts_by_kind.get(row['trial_kind'], 0) + 1
    for configuration in configurations:
        kind = str(configuration['trial_kind'])
        configurations_by_kind[kind] = configurations_by_kind.get(kind, 0) + 1
    summary = {'schema_version': 1, 'experiment': 'E5', 'configurations': len(configurations), 'cases': len(rows), 'dataset': dataset.as_dict(), 'cases_by_trial_kind': counts_by_kind, 'configurations_by_trial_kind': configurations_by_kind, 'sweep_configuration_counts': {tag: sum((1 for configuration in configurations if tag in configuration['sweep_tags'])) for tag in sorted({tag for configuration in configurations for tag in configuration['sweep_tags']})}}
    expected = config.get('expected', {})
    for key in ('configurations', 'cases'):
        if key in expected and int(expected[key]) != int(summary[key]):
            raise ValueError(f'plan changed: {key}={summary[key]}, expected={expected[key]}')
    if 'cases_by_trial_kind' in expected:
        expected_counts = {key: int(value) for key, value in expected['cases_by_trial_kind'].items()}
        if counts_by_kind != expected_counts:
            raise ValueError(f'case-kind counts changed: {counts_by_kind}, expected={expected_counts}')
    return summary

def count_plan(plan_path: str | Path, kind: str='cases') -> int:
    rows = [json.loads(line) for line in Path(plan_path).read_text(encoding='utf-8').splitlines() if line.strip()]
    if kind == 'cases':
        return len(rows)
    if kind == 'configurations':
        return len({row['config_id'] for row in rows})
    if kind == 'query_measurements':
        return sum((int(row.get('measured_queries', 0)) for row in rows if row['trial_kind'] == 'anchor_query'))
    if kind == 'pruned_epoch_inputs':
        return sum((int(row.get('total_epochs', 0)) for row in rows if row['trial_kind'] in {'pruning', 'unsafe_counterexample'}))
    raise ValueError(f'unsupported plan count kind: {kind}')

def dataset_spec_from_config(config_path: str | Path) -> DatasetSpec:
    config = read_json(config_path)
    return DatasetSpec.from_dict(_dataset_fields(config))
