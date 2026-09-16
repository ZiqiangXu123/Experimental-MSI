from __future__ import annotations
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from .constants import ABLATIONS, ATTACK_FAMILIES, FUZZ_SURFACES, VARIANT_MODE
from .util import atomic_write_json, read_jsonl, write_jsonl

def stable_seed(seed: int, *parts: Any) -> int:
    h = hashlib.sha256(seed.to_bytes(8, 'big', signed=False))
    for part in parts:
        data = json.dumps(part, sort_keys=True, separators=(',', ':')).encode('utf-8')
        h.update(len(data).to_bytes(4, 'big'))
        h.update(data)
    return int.from_bytes(h.digest()[:8], 'big') & (1 << 63) - 1

def _case_id(prefix: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return f'{prefix}-{hashlib.sha256(canonical).hexdigest()[:20]}'

def _blocks(cases: list[dict[str, Any]], size: int, prefix: str) -> list[dict[str, Any]]:
    if size < 1:
        raise ValueError('block size must be positive')
    output: list[dict[str, Any]] = []
    for block_index, start in enumerate(range(0, len(cases), size)):
        selected = cases[start:start + size]
        output.append({'schema_version': 1, 'block_id': f'{prefix}-{block_index:05d}', 'block_index': block_index, 'case_indices': list(range(start, start + len(selected))), 'case_ids': [case['case_id'] for case in selected]})
    return output

def _context(base: dict[str, Any], item: dict[str, Any], seed: int, label: str) -> dict[str, Any]:
    value = dict(base)
    value.update(item)
    n = int(value['epoch_length'])
    value.setdefault('query_position', max(1, min(n, n // 2)))
    value.setdefault('version', 1)
    value.setdefault('shard', 11)
    value.setdefault('epoch', 17)
    value.setdefault('anchor_position', 7)
    value.setdefault('anchor_prefix', 64)
    value['fixture_seed'] = stable_seed(seed, 'fixture', label, item)
    return value

def _e7_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config['e7']
    base = dict(section['fixture_base'])
    seed = int(config['seed'])
    contexts = [_context(base, item, seed, f'ctx-{index}') for index, item in enumerate(section['contexts'])]
    if not contexts:
        raise ValueError('E7 requires at least one context')
    deterministic = section['deterministic']
    families = tuple(deterministic.get('families', ATTACK_FAMILIES))
    unknown = set(families) - set(ATTACK_FAMILIES)
    if unknown:
        raise ValueError(f'unknown attack families: {sorted(unknown)}')
    cases: list[dict[str, Any]] = []
    for family in families:
        for context_index, context in enumerate(contexts):
            payload = {'schema_version': 1, 'experiment': 'E7', 'trial_kind': 'deterministic_attack', 'attack_family': family, 'context_index': context_index, 'malicious_responses': int(deterministic['responses_per_family_context']), **context}
            payload['case_seed'] = stable_seed(seed, 'E7', family, context_index)
            payload['case_id'] = _case_id('e7-attack', payload)
            cases.append(payload)
    fuzz = section.get('fuzz', {})
    if bool(fuzz.get('enabled', True)):
        surfaces = tuple(fuzz.get('surfaces', FUZZ_SURFACES))
        if set(surfaces) - set(FUZZ_SURFACES):
            raise ValueError('unknown fuzz surface')
        context_indices = [int(value) for value in fuzz.get('context_indices', range(len(contexts)))]
        for context_index in context_indices:
            context = contexts[context_index]
            for surface in surfaces:
                for replicate in range(int(fuzz['replicates'])):
                    payload = {'schema_version': 1, 'experiment': 'E7', 'trial_kind': 'fuzz', 'surface': surface, 'context_index': context_index, 'fuzz_executions': int(fuzz['executions']), 'fuzz_seed': stable_seed(seed, 'fuzz', context_index, surface, replicate), 'line_trace_interval': int(fuzz.get('line_trace_interval', 100)), 'max_corpus': int(fuzz.get('max_corpus', 4096)), 'replicate': replicate, **context}
                    payload['case_id'] = _case_id('e7-fuzz', payload)
                    cases.append(payload)
    ablation = section.get('ablation', {})
    if bool(ablation.get('enabled', True)):
        values = tuple(ablation.get('ablations', ABLATIONS))
        if set(values) - set(ABLATIONS):
            raise ValueError('unknown ablation')
        context_indices = [int(value) for value in ablation.get('context_indices', range(min(3, len(contexts))))]
        for name in values:
            for context_index in context_indices:
                context = contexts[context_index]
                for replicate in range(int(ablation['replicates'])):
                    payload = {'schema_version': 1, 'experiment': 'E7', 'trial_kind': 'ablation', 'ablation': name, 'context_index': context_index, 'trials': int(ablation['trials']), 'replicate': replicate, **context}
                    payload['fixture_seed'] = stable_seed(seed, 'ablation-fixture', name, context_index, replicate)
                    payload['case_id'] = _case_id('e7-ablation', payload)
                    cases.append(payload)
    return cases

def _availability_case(base: dict[str, Any], values: dict[str, Any], seed: int, sweep: str, replicate: int) -> dict[str, Any]:
    payload = dict(base)
    payload.update(values)
    payload['schema_version'] = 1
    payload['experiment'] = 'E8'
    payload['trial_kind'] = 'availability'
    payload['sweep'] = sweep
    payload['replicate'] = replicate
    payload['mode'] = VARIANT_MODE[str(payload['variant'])]
    payload['fixture_seed'] = stable_seed(seed, 'E8-fixture', payload['variant'], replicate)
    payload['availability_seed'] = stable_seed(seed, 'E8-availability', sweep, values, replicate)
    payload['case_id'] = _case_id('e8', payload)
    return payload

def _e8_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config['e8']
    seed = int(config['seed'])
    base = dict(section['fixture_base'])
    base.update({'measured_queries': int(section['measured_queries']), 'warmup_queries': int(section.get('warmup_queries', 0)), 'client_concurrency': int(section.get('client_concurrency', 32)), 'fast_delay_ms': float(section.get('fast_delay_ms', 2.0)), 'delay_jitter_fraction': float(section.get('delay_jitter_fraction', 0.02)), 'endpoint_timeout_s': float(section.get('endpoint_timeout_s', 60.0))})
    cases: list[dict[str, Any]] = []
    failure = section['failure_sweep']
    for variant in failure['variants']:
        for probability in failure['failure_probabilities']:
            for model in failure['outage_models']:
                for deadline in failure['deadlines_ms']:
                    for replicate in range(int(failure['replicates'])):
                        cases.append(_availability_case(base, {'variant': variant, 'failure_probability': float(probability), 'outage_model': model, 'deadline_ms': float(deadline), 'delay_profile': str(failure.get('delay_profile', 'fast')), 'withholding_behavior': str(failure.get('withholding_behavior', 'silent')), 'retries': int(failure.get('retries', 0))}, seed, 'failure', replicate))
    delay = section.get('delay_sweep', {})
    if bool(delay.get('enabled', True)):
        for variant in delay['variants']:
            for profile in delay['delay_profiles']:
                for deadline in delay['deadlines_ms']:
                    for replicate in range(int(delay['replicates'])):
                        cases.append(_availability_case(base, {'variant': variant, 'failure_probability': float(delay.get('failure_probability', 0.0)), 'outage_model': str(delay.get('outage_model', 'independent')), 'deadline_ms': float(deadline), 'delay_profile': profile, 'withholding_behavior': 'silent', 'retries': 0}, seed, 'delay', replicate))
    retry = section.get('retry_sweep', {})
    if bool(retry.get('enabled', True)):
        for variant in retry['variants']:
            for probability in retry['failure_probabilities']:
                for model in retry['outage_models']:
                    for deadline in retry['deadlines_ms']:
                        for replicate in range(int(retry['replicates'])):
                            cases.append(_availability_case(base, {'variant': variant, 'failure_probability': float(probability), 'outage_model': model, 'deadline_ms': float(deadline), 'delay_profile': str(retry.get('delay_profile', 'fast')), 'withholding_behavior': str(retry.get('withholding_behavior', 'fail_fast')), 'retries': int(retry.get('retries', 1))}, seed, 'retry', replicate))
    return cases

def make_plans(config_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if int(config.get('schema_version', 0)) != 1:
        raise ValueError('unsupported configuration schema')
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    e7_cases = _e7_cases(config)
    e8_cases = _e8_cases(config)
    e7_blocks = _blocks(e7_cases, int(config['e7'].get('block_size', 1)), 'e7-block')
    e8_blocks = _blocks(e8_cases, int(config['e8'].get('block_size', 1)), 'e8-block')
    write_jsonl(output / 'e7_plan.jsonl', e7_cases)
    write_jsonl(output / 'e7_blocks.jsonl', e7_blocks)
    write_jsonl(output / 'e8_plan.jsonl', e8_cases)
    write_jsonl(output / 'e8_blocks.jsonl', e8_blocks)
    attack_counts = {family: sum((int(case['malicious_responses']) for case in e7_cases if case['trial_kind'] == 'deterministic_attack' and case['attack_family'] == family)) for family in ATTACK_FAMILIES}
    deterministic_responses = sum(attack_counts.values())
    fuzz_executions = sum((int(case['fuzz_executions']) for case in e7_cases if case['trial_kind'] == 'fuzz'))
    ablation_trials = sum((int(case['trials']) for case in e7_cases if case['trial_kind'] == 'ablation'))
    e8_queries = sum((int(case['measured_queries']) for case in e8_cases))
    sweeps: dict[str, int] = {}
    for case in e8_cases:
        sweeps[case['sweep']] = sweeps.get(case['sweep'], 0) + 1
    summary = {'schema_version': 1, 'experiment': 'E7+E8', 'label': config.get('label', config_path.stem), 'publication_mode': bool(config.get('publication_mode', False)), 'config_path': str(config_path.resolve()), 'e7_cases': len(e7_cases), 'e7_blocks': len(e7_blocks), 'e7_case_kinds': {kind: sum((1 for case in e7_cases if case['trial_kind'] == kind)) for kind in ('deterministic_attack', 'fuzz', 'ablation')}, 'deterministic_malicious_responses': deterministic_responses, 'paired_honest_controls': deterministic_responses + fuzz_executions, 'malicious_responses_by_family': attack_counts, 'fuzz_executions': fuzz_executions, 'ablation_trials': ablation_trials, 'e8_cases': len(e8_cases), 'e8_blocks': len(e8_blocks), 'e8_sweeps': sweeps, 'e8_measured_queries': e8_queries, 'e8_topology_requirement': str(config['e8'].get('topology_requirement', 'dual-node-preferred'))}
    atomic_write_json(output / 'plan_summary.json', summary)
    atomic_write_json(output / 'resolved_config.json', config)
    return summary

def count_plan(config_path: str | Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='e7-e8-plan-') as directory:
        return make_plans(config_path, directory)
