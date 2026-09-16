from __future__ import annotations
import hashlib
import json
import random
from pathlib import Path
from typing import Any
from .constants import CODECS, DEPLOYMENTS, LAYOUTS, NETWORK_PROFILES, SCHEMES
from .util import atomic_write_text, product_dict, stable_seed

def _digest(obj: Any, length: int=12) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:length]

def load_config(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding='utf-8'))
    if obj.get('schema_version') != 1 or obj.get('experiment') != 'E3':
        raise ValueError('configuration must be schema_version=1 and experiment=E3')
    if not isinstance(obj.get('sweeps'), list) or not obj['sweeps']:
        raise ValueError('configuration requires non-empty sweeps')
    return obj

def _validate_variant(variant: dict[str, Any]) -> None:
    scheme = variant.get('scheme')
    deployment = variant.get('deployment')
    if scheme not in SCHEMES:
        raise ValueError(f'unknown scheme {scheme}')
    if deployment not in DEPLOYMENTS:
        raise ValueError(f'unknown deployment {deployment}')
    if scheme != 'B4_ext' and deployment != 'coalesced':
        raise ValueError('only B4_ext may use split deployment')

def _validate_case(case: dict[str, Any]) -> None:
    _validate_variant(case)
    if case['kind'] not in {'wire_audit', 'network_trial'}:
        raise ValueError('kind must be wire_audit or network_trial')
    if case['profile'] not in NETWORK_PROFILES:
        raise ValueError(f"unknown profile {case['profile']}")
    if case['layout'] not in LAYOUTS or case['codec'] not in CODECS:
        raise ValueError('unknown layout/codec')
    for field in ('epoch_length', 'historical_epochs', 'block_bytes', 'version', 'query_pool_size', 'calibration_pings', 'socket_timeout_s'):
        if float(case[field]) <= 0:
            raise ValueError(f'{field} must be positive')
    if int(case['block_bytes']) < 36:
        raise ValueError('block_bytes is smaller than the canonical header')
    if case['kind'] == 'network_trial':
        if int(case['measured_queries']) < 1 or int(case['warmup_queries']) < 1:
            raise ValueError('network trials require positive warm-up and measured query counts')

def make_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = dict(config.get('defaults', {}))
    required = {'network_trial_count', 'measured_queries', 'warmup_queries', 'query_pool_size', 'calibration_pings', 'socket_timeout_s', 'plan_seed'}
    missing = sorted(required - set(defaults))
    if missing:
        raise ValueError(f'missing defaults: {missing}')
    variants = list(config.get('variants', []))
    if not variants:
        raise ValueError('configuration requires variants')
    for variant in variants:
        _validate_variant(variant)
    blocks: list[dict[str, Any]] = []
    for sweep in config['sweeps']:
        name = str(sweep['name'])
        kind = str(sweep['kind'])
        fixed = dict(sweep.get('fixed', {}))
        factor_rows = list(product_dict(dict(sweep.get('factor_grid', {}))))
        selected_variants = sweep.get('variants', variants)
        trial_count = int(sweep.get('trial_count', 1 if kind == 'wire_audit' else defaults['network_trial_count']))
        for factor in factor_rows:
            base = {'epoch_length': 512, 'historical_epochs': 1000, 'block_bytes': 2048, 'layout': 'epoch_packed', 'codec': 'raw-v1', 'version': 1, 'profile': 'P0', 'shard': 1, **fixed, **factor}
            factor_id = _digest({'sweep': name, **base}, 10)
            for trial in range(trial_count):
                block_seed = stable_seed(defaults['plan_seed'], name, factor_id, trial)
                block_id = f'{name}-{factor_id}-t{trial:02d}'
                cases: list[dict[str, Any]] = []
                for variant in selected_variants:
                    case = {**base, **variant, 'schema_version': 1, 'experiment': 'E3', 'kind': kind, 'sweep': name, 'trial': trial, 'block_id': block_id, 'measured_queries': int(sweep.get('measured_queries', defaults['measured_queries'])), 'warmup_queries': int(sweep.get('warmup_queries', defaults['warmup_queries'])), 'query_pool_size': int(sweep.get('query_pool_size', defaults['query_pool_size'])), 'calibration_pings': int(sweep.get('calibration_pings', defaults['calibration_pings'])), 'socket_timeout_s': float(sweep.get('socket_timeout_s', defaults['socket_timeout_s'])), 'fixture_seed': stable_seed(block_seed, 'fixture', variant['scheme']), 'query_seed': stable_seed(block_seed, 'queries'), 'network_seed': stable_seed(block_seed, 'network', variant['scheme'], variant['deployment']), 'deadlines_ms': list(sweep.get('deadlines_ms', defaults.get('deadlines_ms', [50, 100, 250, 500, 1000, 2000])))}
                    identity = {key: case[key] for key in ('kind', 'sweep', 'trial', 'scheme', 'deployment', 'epoch_length', 'block_bytes', 'profile', 'layout', 'codec')}
                    case['case_id'] = f'{block_id}-{_digest(identity, 12)}'
                    _validate_case(case)
                    cases.append(case)
                blocks.append({'schema_version': 1, 'experiment': 'E3', 'block_id': block_id, 'sweep': name, 'kind': kind, 'trial': trial, 'factor': base, 'case_order_seed': stable_seed(block_seed, 'case-order'), 'cases': cases, 'notes': str(sweep.get('notes', ''))})
    rng = random.Random(int(defaults['plan_seed']))
    rng.shuffle(blocks)
    for index, block in enumerate(blocks):
        block['plan_index'] = index
    ids = [case['case_id'] for block in blocks for case in block['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('case IDs are not unique')
    return blocks

def write_plan(path: Path, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    text = ''.join((json.dumps(block, sort_keys=True, separators=(',', ':')) + '\n' for block in blocks))
    atomic_write_text(path, text)
    cases = [case for block in blocks for case in block['cases']]
    network_by_config: dict[str, int] = {}
    for case in cases:
        if case['kind'] != 'network_trial':
            continue
        key = _digest({field: case[field] for field in ('sweep', 'scheme', 'deployment', 'epoch_length', 'block_bytes', 'profile', 'layout', 'codec')}, 20)
        network_by_config[key] = network_by_config.get(key, 0) + 1
    return {'blocks': len(blocks), 'cases': len(cases), 'wire_audit_cases': sum((c['kind'] == 'wire_audit' for c in cases)), 'network_trial_cases': sum((c['kind'] == 'network_trial' for c in cases)), 'network_configurations': len(network_by_config), 'minimum_network_trials_per_configuration': min(network_by_config.values()) if network_by_config else 0, 'maximum_network_trials_per_configuration': max(network_by_config.values()) if network_by_config else 0, 'total_measured_network_queries': sum((int(c['measured_queries']) for c in cases if c['kind'] == 'network_trial'))}

def read_plan(path: Path) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            block = json.loads(line)
            if block.get('schema_version') != 1 or block.get('experiment') != 'E3':
                raise ValueError(f'invalid plan row at line {line_number}')
            blocks.append(block)
    return blocks
