from __future__ import annotations
import hashlib
import json
import random
from pathlib import Path
from typing import Any
from .constants import ABLATIONS, CODECS, LAYOUTS, SCHEMES
from .util import atomic_write_text, next_power_of_two, product_dict, stable_seed

def _short_digest(obj: Any, length: int=12) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:length]

def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding='utf-8'))
    if config.get('schema_version') != 1:
        raise ValueError('configuration schema_version must be 1')
    if config.get('experiment') != 'E2':
        raise ValueError('configuration experiment must be E2')
    if not isinstance(config.get('sweeps'), list) or not config['sweeps']:
        raise ValueError('configuration must contain non-empty sweeps')
    return config

def _validate_case(case: dict[str, Any]) -> None:
    if case['scheme'] not in SCHEMES:
        raise ValueError(f"unknown scheme {case['scheme']}")
    if case['ablation'] not in ABLATIONS:
        raise ValueError(f"unknown ablation {case['ablation']}")
    if case['layout'] not in LAYOUTS:
        raise ValueError(f"unknown layout {case['layout']}")
    if case['codec'] not in CODECS:
        raise ValueError(f"unknown codec {case['codec']}")
    for field in ('epoch_length', 'historical_epochs', 'block_bytes', 'version', 'measured_queries', 'warmup_queries', 'phase_queries', 'allocation_queries', 'query_pool_size'):
        if int(case[field]) < 1:
            raise ValueError(f'{field} must be positive')
    if case['scheme'] == 'B0_raw' and case['ablation'] != 'safe':
        raise ValueError('B0_raw has no cryptographic ablation')

def make_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = dict(config.get('defaults', {}))
    required_defaults = {'trial_count', 'measured_queries', 'warmup_queries', 'phase_queries', 'allocation_queries', 'query_pool_size', 'plan_seed'}
    missing = sorted(required_defaults - set(defaults))
    if missing:
        raise ValueError(f'missing defaults: {missing}')
    blocks: list[dict[str, Any]] = []
    for sweep in config['sweeps']:
        name = str(sweep['name'])
        fixed = dict(sweep.get('fixed', {}))
        factor_grid = dict(sweep.get('factor_grid', {}))
        case_grid = dict(sweep.get('case_grid', {}))
        trial_count = int(sweep.get('trial_count', defaults['trial_count']))
        if trial_count < 1:
            raise ValueError(f'trial_count for {name} must be positive')
        factor_rows = list(product_dict(factor_grid))
        case_rows = list(product_dict(case_grid))
        if not case_rows:
            raise ValueError(f'sweep {name} has no cases')
        for factor in factor_rows:
            base = {'epoch_length': 512, 'historical_epochs': 1000, 'block_bytes': 2048, 'layout': 'epoch_packed', 'codec': 'raw-v1', 'version': 1, 'shard': 1, **fixed, **factor}
            factor_digest = _short_digest({'sweep': name, **base}, 10)
            for trial in range(trial_count):
                block_seed = stable_seed(defaults['plan_seed'], name, factor_digest, trial)
                block_id = f'{name}-{factor_digest}-t{trial:02d}'
                cases: list[dict[str, Any]] = []
                for case_row in case_rows:
                    case = {**base, **case_row, 'experiment': 'E2', 'sweep': name, 'trial': trial, 'block_id': block_id, 'measured_queries': int(sweep.get('measured_queries', defaults['measured_queries'])), 'warmup_queries': int(sweep.get('warmup_queries', defaults['warmup_queries'])), 'phase_queries': int(sweep.get('phase_queries', defaults['phase_queries'])), 'allocation_queries': int(sweep.get('allocation_queries', defaults['allocation_queries'])), 'query_pool_size': int(sweep.get('query_pool_size', defaults['query_pool_size'])), 'fixture_seed': stable_seed(block_seed, 'fixture'), 'query_seed': stable_seed(block_seed, 'queries')}
                    n_prime = next_power_of_two(int(case['epoch_length']))
                    if case['scheme'] == 'B3_leaf' and n_prime >= 4096:
                        case['phase_queries'] = min(case['phase_queries'], 64)
                        case['allocation_queries'] = min(case['allocation_queries'], 8)
                    identity = {key: case[key] for key in ('sweep', 'trial', 'scheme', 'ablation', 'epoch_length', 'historical_epochs', 'block_bytes', 'layout', 'codec', 'version')}
                    case['case_id'] = f'{block_id}-{_short_digest(identity, 12)}'
                    _validate_case(case)
                    cases.append(case)
                block = {'schema_version': 1, 'experiment': 'E2', 'block_id': block_id, 'sweep': name, 'trial': trial, 'factor': base, 'case_order_seed': stable_seed(block_seed, 'case-order'), 'cases': cases, 'notes': sweep.get('notes', '')}
                blocks.append(block)
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
    cases = sum((len(block['cases']) for block in blocks))
    launches_by_configuration: dict[str, int] = {}
    for block in blocks:
        for case in block['cases']:
            config_key = _short_digest({key: case[key] for key in ('sweep', 'scheme', 'ablation', 'epoch_length', 'historical_epochs', 'block_bytes', 'layout', 'codec')}, 20)
            launches_by_configuration[config_key] = launches_by_configuration.get(config_key, 0) + 1
    return {'blocks': len(blocks), 'cases': cases, 'minimum_launches_per_configuration': min(launches_by_configuration.values()), 'maximum_launches_per_configuration': max(launches_by_configuration.values())}

def read_plan(path: Path) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            block = json.loads(line)
            if block.get('schema_version') != 1 or block.get('experiment') != 'E2':
                raise ValueError(f'invalid plan row at line {line_number}')
            blocks.append(block)
    return blocks
