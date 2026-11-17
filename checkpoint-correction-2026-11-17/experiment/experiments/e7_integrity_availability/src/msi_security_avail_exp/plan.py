from __future__ import annotations
import copy
import itertools
import math
from pathlib import Path
from typing import Any
from .constants import ABLATIONS, ATTACK_FAMILIES, AVAILABILITY_VARIANTS, FUZZ_SURFACES, VARIANT_MODE
from .util import atomic_write_json, load_json, write_jsonl

def _require_int(value: Any, name: str, minimum: int=1) -> int:
    out = int(value)
    if out < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return out

def _contexts(section: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = section.get(key)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'e7.{key} must be a non-empty list')
    required = {'name', 'epoch_length', 'block_bytes', 'mode', 'anchor_mode', 'layout', 'codec'}
    output: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError(f'each e7.{key} row must contain {sorted(required)}')
        row = copy.deepcopy(raw)
        name = str(row['name'])
        if name in names:
            raise ValueError(f'duplicate context name: {name}')
        names.add(name)
        row['name'] = name
        row['epoch_length'] = _require_int(row['epoch_length'], f'{name}.epoch_length')
        row['block_bytes'] = _require_int(row['block_bytes'], f'{name}.block_bytes', 36)
        row['query_position'] = max(1, min(row['epoch_length'], int(row.get('query_position', max(1, row['epoch_length'] // 2)))))
        row['fixture_seed'] = int(row.get('fixture_seed', 7))
        row['anchor_prefix'] = int(row.get('anchor_prefix', 64))
        if row['mode'] not in {'full', 'leaf', 'ext'}:
            raise ValueError(f'unsupported mode in {name}')
        if row['anchor_mode'] not in {'direct', 'aggregate'}:
            raise ValueError(f'unsupported anchor mode in {name}')
        output.append(row)
    return output

def _e7_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config['e7']
    deterministic_contexts = _contexts(section, 'contexts')
    fuzz_contexts = _contexts(section, 'fuzz_contexts') if section.get('fuzz_contexts') else deterministic_contexts
    deterministic_replicates = _require_int(section['deterministic_replicates'], 'deterministic_replicates')
    deterministic_trials = _require_int(section['deterministic_trials_per_block'], 'deterministic_trials_per_block')
    fuzz_replicates = _require_int(section['fuzz_replicates'], 'fuzz_replicates')
    fuzz_trials = _require_int(section['fuzz_trials_per_block'], 'fuzz_trials_per_block')
    line_trials = int(section.get('line_coverage_trials_per_block', min(100, fuzz_trials)))
    ablation_replicates = _require_int(section['ablation_replicates'], 'ablation_replicates')
    ablation_trials = _require_int(section['ablation_trials_per_block'], 'ablation_trials_per_block')
    base_seed = int(section.get('seed', 700000))
    cases: list[dict[str, Any]] = []
    index = 0
    for family, context, replicate in itertools.product(ATTACK_FAMILIES, deterministic_contexts, range(deterministic_replicates)):
        cases.append({'case_id': f'e7-{index:06d}', 'kind': 'deterministic_attack', 'family': family, 'context': copy.deepcopy(context), 'replicate': replicate, 'trials': deterministic_trials, 'seed': base_seed + index * 10368889 + replicate})
        index += 1
    for surface, context, replicate in itertools.product(FUZZ_SURFACES, fuzz_contexts, range(fuzz_replicates)):
        cases.append({'case_id': f'e7-{index:06d}', 'kind': 'greybox_fuzz', 'surface': surface, 'context': copy.deepcopy(context), 'replicate': replicate, 'trials': fuzz_trials, 'line_coverage_trials': max(0, min(fuzz_trials, line_trials)), 'seed': base_seed + index * 3518319154 + replicate})
        index += 1
    ablation_context_map = section.get('ablation_context_map', {})
    by_name = {row['name']: row for row in deterministic_contexts}
    default_context = deterministic_contexts[0]
    for ablation, replicate in itertools.product(ABLATIONS, range(ablation_replicates)):
        context_name = ablation_context_map.get(ablation)
        context = by_name.get(context_name, default_context)
        cases.append({'case_id': f'e7-{index:06d}', 'kind': 'unsafe_ablation', 'ablation': ablation, 'context': copy.deepcopy(context), 'replicate': replicate, 'trials': ablation_trials, 'seed': base_seed + index * 2496678331 + replicate})
        index += 1
    return cases

def _e8_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config['e8']
    n_values = [int(x) for x in section['epoch_lengths']]
    variants = [str(x) for x in section.get('variants', AVAILABILITY_VARIANTS)]
    probabilities = [float(x) for x in section['failure_probabilities']]
    outage_models = [str(x) for x in section['outage_models']]
    deadlines = [float(x) for x in section['deadlines_ms']]
    retry_policies = [str(x) for x in section['retry_policies']]
    replicates = _require_int(section['replicates'], 'e8.replicates')
    queries = _require_int(section['queries_per_case'], 'e8.queries_per_case')
    base_seed = int(section.get('seed', 800000))
    block_bytes = int(section.get('block_bytes', 2048))
    anchor_mode = str(section.get('anchor_mode', 'direct'))
    if anchor_mode not in {'direct', 'aggregate'}:
        raise ValueError('e8.anchor_mode must be direct or aggregate')
    for n in n_values:
        _require_int(n, 'epoch length')
    for variant in variants:
        if variant not in AVAILABILITY_VARIANTS:
            raise ValueError(f'unsupported availability variant {variant}')
    for p in probabilities:
        if not 0 <= p <= 1:
            raise ValueError('failure probabilities must be in [0,1]')
    for model in outage_models:
        if model not in {'independent', 'correlated'}:
            raise ValueError('outage model must be independent or correlated')
    for deadline in deadlines:
        if deadline <= 0:
            raise ValueError('deadlines must be positive')
    for policy in retry_policies:
        if policy not in {'no_retry', 'one_retry'}:
            raise ValueError('retry policy must be no_retry or one_retry')
    cases: list[dict[str, Any]] = []
    index = 0
    for n, variant in itertools.product(n_values, variants):
        for p, model, deadline, retry, replicate in itertools.product(probabilities, outage_models, deadlines, retry_policies, range(replicates)):
            cases.append({'case_id': f'e8-{index:07d}', 'kind': 'availability', 'epoch_length': n, 'block_bytes': block_bytes, 'mode': VARIANT_MODE[variant], 'variant': variant, 'failure_probability': p, 'outage_model': model, 'deadline_ms': deadline, 'retry_policy': retry, 'replicate': replicate, 'queries': queries, 'anchor_mode': anchor_mode, 'layout': str(section.get('layout', 'per_block')), 'codec': str(section.get('codec', 'raw-v1')), 'query_position': max(1, min(n, int(section.get('query_position', max(1, n // 2))))), 'fixture_seed': int(section.get('fixture_seed', 77)) + n, 'seed': base_seed + index * 10368889 + replicate})
            index += 1
    return cases

def _blocks(cases: list[dict[str, Any]], prefix: str, size: int, *, preserve_group: str | None=None) -> list[dict[str, Any]]:
    if size < 1:
        raise ValueError('block size must be positive')
    output: list[dict[str, Any]] = []
    if preserve_group is None:
        groups = [(None, cases)]
    else:
        grouped: dict[Any, list[dict[str, Any]]] = {}
        order: list[Any] = []
        for case in cases:
            if preserve_group == 'fixture':
                key = (case.get('epoch_length'), case.get('variant'), case.get('anchor_mode'), case.get('block_bytes'))
            else:
                key = case.get(preserve_group)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(case)
        groups = [(key, grouped[key]) for key in order]
    block_index = 0
    for key, group in groups:
        for offset in range(0, len(group), size):
            subset = group[offset:offset + size]
            output.append({'block_id': f'{prefix}-block-{block_index:06d}', 'block_index': block_index, 'case_ids': [row['case_id'] for row in subset], 'group': key})
            block_index += 1
    return output

def build_plan(config_path: Path, output_dir: Path) -> dict[str, Any]:
    config = load_json(config_path)
    if not isinstance(config, dict) or 'e7' not in config or 'e8' not in config:
        raise ValueError('configuration must contain e7 and e8 sections')
    output_dir.mkdir(parents=True, exist_ok=True)
    e7_cases = _e7_cases(config)
    e8_cases = _e8_cases(config)
    e7_block_size = int(config['e7'].get('cases_per_block', 1))
    e8_block_size = int(config['e8'].get('cases_per_block', 20))
    e7_blocks = _blocks(e7_cases, 'e7', e7_block_size)
    e8_blocks = _blocks(e8_cases, 'e8', e8_block_size, preserve_group='fixture')
    deterministic = [row for row in e7_cases if row['kind'] == 'deterministic_attack']
    fuzz = [row for row in e7_cases if row['kind'] == 'greybox_fuzz']
    ablation = [row for row in e7_cases if row['kind'] == 'unsafe_ablation']
    scenarios_by_family = {family: sum((int(row['trials']) for row in deterministic if row['family'] == family)) for family in ATTACK_FAMILIES}
    malicious_responses_by_family = dict(scenarios_by_family)
    malicious_responses_by_family['equivocation'] *= 2
    summary = {'experiment': 'E7+E8', 'label': str(config.get('label', config_path.stem)), 'publication_eligible': bool(config.get('publication_eligible', False)), 'e7_cases': len(e7_cases), 'e7_blocks': len(e7_blocks), 'e7_deterministic_blocks': len(deterministic), 'e7_fuzz_blocks': len(fuzz), 'e7_ablation_blocks': len(ablation), 'e7_attack_scenarios': sum(scenarios_by_family.values()), 'e7_malicious_responses': sum(malicious_responses_by_family.values()) + sum((int(row['trials']) for row in fuzz)), 'e7_honest_controls': sum(malicious_responses_by_family.values()) + sum((int(row['trials']) for row in fuzz + ablation)), 'e7_scenarios_by_family': scenarios_by_family, 'e7_malicious_responses_by_family': malicious_responses_by_family, 'e7_fuzz_trials': sum((int(row['trials']) for row in fuzz)), 'e7_ablation_trials': sum((int(row['trials']) for row in ablation)), 'e8_cases': len(e8_cases), 'e8_blocks': len(e8_blocks), 'e8_simulated_queries': sum((int(row['queries']) for row in e8_cases)), 'e8_unique_fixture_keys': len({(row['epoch_length'], row['variant'], row['anchor_mode'], row['block_bytes']) for row in e8_cases}), 'config_name': config_path.name}
    atomic_write_json(output_dir / 'resolved_config.json', config)
    write_jsonl(output_dir / 'e7_cases.jsonl', e7_cases)
    write_jsonl(output_dir / 'e7_blocks.jsonl', e7_blocks)
    write_jsonl(output_dir / 'e8_cases.jsonl', e8_cases)
    write_jsonl(output_dir / 'e8_blocks.jsonl', e8_blocks)
    atomic_write_json(output_dir / 'plan_summary.json', summary)
    return summary
