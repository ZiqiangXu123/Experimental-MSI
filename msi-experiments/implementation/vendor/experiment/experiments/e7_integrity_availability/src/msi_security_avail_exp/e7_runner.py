from __future__ import annotations
import collections
import resource
import time
from pathlib import Path
from typing import Any
from .attacks import build_attack_environment, make_ablation_attack, make_attack
from .fixture import honest_frame
from .crypto import Ed25519OpenSSL
from .fuzz import run_fuzz_case
from .pipeline import verify_frame
from .util import atomic_write_gzip_json, read_jsonl, system_info

def _counter_add(counter: dict[str, int], key: str, amount: int=1) -> None:
    counter[key] = counter.get(key, 0) + amount

def _usage() -> dict[str, float | int]:
    value = resource.getrusage(resource.RUSAGE_SELF)
    return {'user_cpu_s': float(value.ru_utime), 'system_cpu_s': float(value.ru_stime), 'maxrss_raw': int(value.ru_maxrss), 'minor_faults': int(value.ru_minflt), 'major_faults': int(value.ru_majflt), 'voluntary_context_switches': int(value.ru_nvcsw), 'involuntary_context_switches': int(value.ru_nivcsw)}

def _delta(before: dict[str, float | int], after: dict[str, float | int]) -> dict[str, float | int]:
    out: dict[str, float | int] = {}
    for key in before:
        if key == 'maxrss_raw':
            out[key] = after[key]
        else:
            out[key] = after[key] - before[key]
    return out

def _run_deterministic(case: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter_ns()
    before = _usage()
    context = dict(case['context'])
    context['fixture_seed'] = int(context.get('fixture_seed', 7)) + int(case['replicate']) * 1009
    env = build_attack_environment(context)
    backend = Ed25519OpenSSL()
    honest_q = (env.target.shard, env.target.epoch, env.position)
    honest = honest_frame(env.target, env.position)
    trials = int(case['trials'])
    family = str(case['family'])
    malicious_latencies: list[int] = []
    honest_latencies: list[int] = []
    stages: dict[str, int] = {}
    reasons: dict[str, int] = {}
    expected: dict[str, int] = {}
    operators: dict[str, int] = {}
    guard_coverage: set[str] = set()
    false_accepts = false_rejects = crashes = stage_mismatches = 0
    malicious_responses = 0
    metadata_violation_accepts = 0
    for trial in range(trials):
        mutation = make_attack(env, family, trial + int(case['seed']))
        _counter_add(operators, mutation.mutation_name)
        for frame_index, frame in enumerate(mutation.frames):
            malicious_responses += 1
            honest_decision = verify_frame(honest, honest_q, env.target, openssl=backend)
            honest_latencies.append(honest_decision.latency_ns)
            if not honest_decision.accepted:
                false_rejects += 1
            if honest_decision.stage == 'Crash':
                crashes += 1
            decision = verify_frame(frame, mutation.query, env.target, openssl=backend)
            malicious_latencies.append(decision.latency_ns)
            _counter_add(stages, decision.stage)
            _counter_add(reasons, decision.reason)
            guard_coverage.update(decision.guards)
            expected_stage = mutation.expected_stages[frame_index]
            if expected_stage is not None:
                _counter_add(expected, expected_stage)
                if decision.stage != expected_stage:
                    stage_mismatches += 1
            if decision.accepted:
                false_accepts += 1
                if decision.metadata_violation:
                    metadata_violation_accepts += 1
            if decision.stage == 'Crash':
                crashes += 1
    after = _usage()
    return {'case_id': case['case_id'], 'kind': case['kind'], 'family': family, 'context': context, 'replicate': case['replicate'], 'seed': case['seed'], 'attack_scenarios': trials, 'malicious_responses': malicious_responses, 'honest_controls': malicious_responses, 'false_accepts': false_accepts, 'false_rejects': false_rejects, 'crashes': crashes, 'stage_mismatches': stage_mismatches, 'metadata_violation_accepts': metadata_violation_accepts, 'stage_counts': stages, 'expected_stage_counts': expected, 'reason_counts': reasons, 'operator_counts': operators, 'guard_coverage': sorted(guard_coverage), 'malicious_latencies_ns': malicious_latencies, 'honest_latencies_ns': honest_latencies, 'resource_delta': _delta(before, after), 'runtime_ns': time.perf_counter_ns() - start, 'checks': {'all_honest_accepted': false_rejects == 0, 'no_false_accept': false_accepts == 0, 'no_crash': crashes == 0, 'earliest_gate_exact': stage_mismatches == 0}}

def _run_fuzz(case: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter_ns()
    before = _usage()
    context = dict(case['context'])
    context['fixture_seed'] = int(context.get('fixture_seed', 7)) + int(case['replicate']) * 1009
    fuzz_case = dict(context)
    fuzz_case.update({'surface': str(case['surface']), 'fuzz_executions': int(case['trials']), 'fuzz_seed': int(case['seed']), 'line_trace_interval': max(1, int(case['trials']) // max(1, int(case.get('line_coverage_trials', 1)))), 'max_corpus': 4096})
    result = run_fuzz_case(fuzz_case)
    metrics = result['metrics']
    after = _usage()
    return {'case_id': case['case_id'], 'kind': case['kind'], 'surface': case['surface'], 'context': context, 'replicate': case['replicate'], 'seed': case['seed'], 'attack_scenarios': int(metrics['fuzz_executions']), 'malicious_responses': int(metrics['fuzz_executions']), 'honest_controls': int(metrics['paired_honest_controls']), 'false_accepts': int(metrics['false_accepts']), 'false_rejects': int(metrics['false_rejects']), 'crashes': int(metrics['crashes']), 'accepted_mutations': int(metrics['accepted_mutations']), 'stage_counts': result['stage_counts'], 'reason_counts': result['reason_counts'], 'guard_coverage': result['covered_guards'], 'coverage_signatures': int(metrics['new_coverage_events']), 'corpus_size': int(metrics['corpus_size']), 'line_points_covered': int(metrics['line_points_covered']), 'line_points_total': int(metrics['line_points_total']), 'sampled_line_coverage': float(metrics['sampled_line_coverage']), 'semantic_guard_coverage': float(metrics['semantic_guard_coverage']), 'covered_line_points': result['covered_line_points'], 'line_universe': result['line_universe'], 'interesting_inputs': result['interesting_inputs'], 'resource_delta': _delta(before, after), 'runtime_ns': time.perf_counter_ns() - start, 'checks': {'all_honest_accepted': int(metrics['false_rejects']) == 0, 'no_false_accept': int(metrics['false_accepts']) == 0, 'no_crash': int(metrics['crashes']) == 0, 'budget_complete': bool(result['checks']['budget_complete'])}}

def _run_ablation(case: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter_ns()
    before = _usage()
    context = dict(case['context'])
    context['fixture_seed'] = int(context.get('fixture_seed', 7)) + int(case['replicate']) * 1009
    env = build_attack_environment(context)
    backend = Ed25519OpenSSL()
    trials = int(case['trials'])
    ablation = str(case['ablation'])
    mutation = make_ablation_attack(env, ablation)
    unsafe_fixture = mutation.unsafe_fixture or env.target
    honest_q = (env.target.shard, env.target.epoch, env.position)
    honest = honest_frame(env.target, env.position)
    safe_accepts = unsafe_accepts = honest_false_rejects = crashes = 0
    safe_stages: dict[str, int] = {}
    unsafe_stages: dict[str, int] = {}
    safe_reasons: dict[str, int] = {}
    unsafe_reasons: dict[str, int] = {}
    safe_latencies: list[int] = []
    unsafe_latencies: list[int] = []
    honest_latencies: list[int] = []
    guard_coverage: set[str] = set()
    for _ in range(trials):
        honest_decision = verify_frame(honest, honest_q, env.target, openssl=backend)
        honest_latencies.append(honest_decision.latency_ns)
        if not honest_decision.accepted:
            honest_false_rejects += 1
        if honest_decision.stage == 'Crash':
            crashes += 1
        for frame in mutation.frames:
            safe = verify_frame(frame, mutation.query, unsafe_fixture, ablation='safe', openssl=backend)
            unsafe = verify_frame(frame, mutation.query, unsafe_fixture, ablation=mutation.unsafe_ablation or ablation, openssl=backend)
            safe_latencies.append(safe.latency_ns)
            unsafe_latencies.append(unsafe.latency_ns)
            guard_coverage.update(safe.guards)
            guard_coverage.update(unsafe.guards)
            _counter_add(safe_stages, safe.stage)
            _counter_add(unsafe_stages, unsafe.stage)
            _counter_add(safe_reasons, safe.reason)
            _counter_add(unsafe_reasons, unsafe.reason)
            if safe.accepted:
                safe_accepts += 1
            if unsafe.accepted:
                unsafe_accepts += 1
            if safe.stage == 'Crash' or unsafe.stage == 'Crash':
                crashes += 1
    expected_attack_responses = trials * len(mutation.frames)
    after = _usage()
    return {'case_id': case['case_id'], 'kind': case['kind'], 'ablation': ablation, 'mutation_name': mutation.mutation_name, 'context': context, 'replicate': case['replicate'], 'seed': case['seed'], 'attack_scenarios': trials, 'malicious_responses': expected_attack_responses, 'honest_controls': trials, 'safe_accepts': safe_accepts, 'unsafe_accepts': unsafe_accepts, 'honest_false_rejects': honest_false_rejects, 'crashes': crashes, 'safe_stage_counts': safe_stages, 'unsafe_stage_counts': unsafe_stages, 'safe_reason_counts': safe_reasons, 'unsafe_reason_counts': unsafe_reasons, 'guard_coverage': sorted(guard_coverage), 'safe_latencies_ns': safe_latencies, 'unsafe_latencies_ns': unsafe_latencies, 'honest_latencies_ns': honest_latencies, 'resource_delta': _delta(before, after), 'runtime_ns': time.perf_counter_ns() - start, 'checks': {'safe_pipeline_rejects_attack': safe_accepts == 0, 'unsafe_ablation_accepts_attack': unsafe_accepts == expected_attack_responses, 'honest_control_accepts': honest_false_rejects == 0, 'no_crash': crashes == 0}}

def run_case(case: dict[str, Any]) -> dict[str, Any]:
    kind = case['kind']
    if kind == 'deterministic_attack':
        return _run_deterministic(case)
    if kind == 'greybox_fuzz':
        return _run_fuzz(case)
    if kind == 'unsafe_ablation':
        return _run_ablation(case)
    raise ValueError(f'unsupported E7 case kind: {kind}')

def run_block(plan_dir: Path, block_index: int, raw_dir: Path) -> Path:
    cases = {row['case_id']: row for row in read_jsonl(plan_dir / 'e7_cases.jsonl')}
    blocks = read_jsonl(plan_dir / 'e7_blocks.jsonl')
    if not 0 <= block_index < len(blocks):
        raise IndexError(f'E7 block index {block_index} outside [0,{len(blocks) - 1}]')
    block = blocks[block_index]
    started = time.time_ns()
    results = [run_case(cases[case_id]) for case_id in block['case_ids']]
    payload = {'schema_version': 1, 'experiment': 'E7', 'block_id': block['block_id'], 'block_index': block_index, 'case_ids': block['case_ids'], 'started_unix_ns': started, 'finished_unix_ns': time.time_ns(), 'environment': system_info(), 'results': results, 'checks': {'case_count_matches': len(results) == len(block['case_ids']), 'case_ids_match': [row['case_id'] for row in results] == block['case_ids'], 'all_case_checks_pass': all((all((bool(v) for v in row['checks'].values())) for row in results))}}
    path = raw_dir / f'e7-block-{block_index:06d}.json.gz'
    atomic_write_gzip_json(path, payload)
    return path
