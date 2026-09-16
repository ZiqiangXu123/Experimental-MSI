from __future__ import annotations
import collections
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable
from .constants import ATTACK_FAMILIES, GUARD_IDS
from .svg import bar_chart, line_chart, stacked_bar
from .util import atomic_write_json, atomic_write_text, bootstrap_median_ci, load_json, median, percentile, read_gzip_json, read_jsonl, upper_zero_event_bound, validate_manifest, write_csv, write_manifest
_GATE_ORDER = ('Fetch', 'CheckResp', 'ResolveWitness', 'VerifyMember', 'VerifyAnchor', 'Accept', 'Crash')

def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

def _finite(value: float | int | None) -> float | int | str:
    if value is None:
        return ''
    if isinstance(value, float) and (not math.isfinite(value)):
        return ''
    return value

def inspect_blocks(plan_dir: Path, raw_dir: Path, experiment: str) -> dict[str, Any]:
    prefix = experiment.lower()
    blocks = read_jsonl(plan_dir / f'{prefix}_blocks.jsonl')
    expected_by_index = {int(row['block_index']): row for row in blocks}
    valid_payloads: list[dict[str, Any]] = []
    missing: list[int] = []
    corrupt: list[dict[str, Any]] = []
    for index, block in expected_by_index.items():
        path = raw_dir / f'{prefix}-block-{index:06d}.json.gz'
        if not path.exists():
            missing.append(index)
            continue
        try:
            payload = read_gzip_json(path)
            if payload.get('block_index') != index:
                raise ValueError('block_index mismatch')
            if payload.get('block_id') != block['block_id']:
                raise ValueError('block_id mismatch')
            if payload.get('case_ids') != block['case_ids']:
                raise ValueError('case_ids mismatch')
            checks = payload.get('checks', {})
            if not checks or not all((bool(v) for v in checks.values())):
                raise ValueError(f'block checks failed: {checks}')
            if [row.get('case_id') for row in payload.get('results', [])] != block['case_ids']:
                raise ValueError('result case IDs mismatch')
            valid_payloads.append(payload)
        except Exception as exc:
            corrupt.append({'block_index': index, 'file': path.name, 'error': f'{type(exc).__name__}: {exc}'})
    pattern = re.compile(f'^{re.escape(prefix)}-block-(\\d{{6}})\\.json\\.gz$')
    extra: list[str] = []
    if raw_dir.exists():
        for path in raw_dir.iterdir():
            match = pattern.match(path.name)
            if match and int(match.group(1)) not in expected_by_index:
                extra.append(path.name)
    return {'experiment': experiment, 'expected_blocks': len(blocks), 'valid_blocks': len(valid_payloads), 'missing_indices': missing, 'corrupt_blocks': corrupt, 'extra_files': sorted(extra), 'complete': not missing and (not corrupt) and (not extra) and (len(valid_payloads) == len(blocks)), 'payloads': sorted(valid_payloads, key=lambda row: int(row['block_index']))}

def missing_block_indices(plan_dir: Path, raw_dir: Path, experiment: str) -> list[int]:
    audit = inspect_blocks(plan_dir, raw_dir, experiment)
    values = set(audit['missing_indices'])
    values.update((int(row['block_index']) for row in audit['corrupt_blocks']))
    return sorted(values)

def _flatten_results(payloads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [result for payload in payloads for result in payload['results']]

def _e7_analysis(results: list[dict[str, Any]], output: Path, plan_summary: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deterministic = [row for row in results if row['kind'] == 'deterministic_attack']
    fuzz = [row for row in results if row['kind'] == 'greybox_fuzz']
    ablations = [row for row in results if row['kind'] == 'unsafe_ablation']
    case_rows: list[dict[str, Any]] = []
    family_acc: dict[str, dict[str, Any]] = {}
    all_false_accepts = all_false_rejects = all_crashes = 0
    all_stage_mismatches = 0
    for row in deterministic:
        family = row['family']
        acc = family_acc.setdefault(family, {'scenarios': 0, 'malicious': 0, 'controls': 0, 'false_accepts': 0, 'false_rejects': 0, 'crashes': 0, 'stage_mismatches': 0, 'stage_counts': collections.Counter(), 'reason_counts': collections.Counter(), 'operators': collections.Counter(), 'malicious_latencies': [], 'honest_latencies': [], 'guards': set()})
        for key, source in (('scenarios', 'attack_scenarios'), ('malicious', 'malicious_responses'), ('controls', 'honest_controls'), ('false_accepts', 'false_accepts'), ('false_rejects', 'false_rejects'), ('crashes', 'crashes'), ('stage_mismatches', 'stage_mismatches')):
            acc[key] += int(row[source])
        acc['stage_counts'].update(row['stage_counts'])
        acc['reason_counts'].update(row['reason_counts'])
        acc['operators'].update(row['operator_counts'])
        acc['malicious_latencies'].extend(row['malicious_latencies_ns'])
        acc['honest_latencies'].extend(row['honest_latencies_ns'])
        acc['guards'].update(row['guard_coverage'])
        all_false_accepts += int(row['false_accepts'])
        all_false_rejects += int(row['false_rejects'])
        all_crashes += int(row['crashes'])
        all_stage_mismatches += int(row['stage_mismatches'])
        case_rows.append({'case_id': row['case_id'], 'kind': row['kind'], 'family': family, 'surface': '', 'ablation': '', 'context': row['context']['name'], 'mode': row['context']['mode'], 'anchor_mode': row['context']['anchor_mode'], 'replicate': row['replicate'], 'attack_scenarios': row['attack_scenarios'], 'malicious_responses': row['malicious_responses'], 'honest_controls': row['honest_controls'], 'false_accepts': row['false_accepts'], 'false_rejects': row['false_rejects'], 'crashes': row['crashes'], 'stage_mismatches': row['stage_mismatches'], 'rejection_p50_ms': percentile(row['malicious_latencies_ns'], 0.5) / 1000000.0, 'rejection_p99_ms': percentile(row['malicious_latencies_ns'], 0.99) / 1000000.0, 'runtime_s': row['runtime_ns'] / 1000000000.0})
    family_rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    latency_rows: list[dict[str, Any]] = []
    for family in ATTACK_FAMILIES:
        acc = family_acc.get(family, {'scenarios': 0, 'malicious': 0, 'controls': 0, 'false_accepts': 0, 'false_rejects': 0, 'crashes': 0, 'stage_mismatches': 0, 'stage_counts': collections.Counter(), 'reason_counts': collections.Counter(), 'operators': collections.Counter(), 'malicious_latencies': [], 'honest_latencies': [], 'guards': set()})
        malicious = int(acc['malicious'])
        family_rows.append({'family': family, 'attack_scenarios': acc['scenarios'], 'malicious_responses': malicious, 'paired_honest_controls': acc['controls'], 'false_accepts': acc['false_accepts'], 'false_rejects': acc['false_rejects'], 'crashes': acc['crashes'], 'stage_mismatches': acc['stage_mismatches'], 'zero_event_95pct_upper_bound': upper_zero_event_bound(malicious) if acc['false_accepts'] == 0 else '', 'rejection_p50_ms': percentile(acc['malicious_latencies'], 0.5) / 1000000.0, 'rejection_p95_ms': percentile(acc['malicious_latencies'], 0.95) / 1000000.0, 'rejection_p99_ms': percentile(acc['malicious_latencies'], 0.99) / 1000000.0, 'honest_p50_ms': percentile(acc['honest_latencies'], 0.5) / 1000000.0, 'guard_count': len(acc['guards']), 'operator_counts': _json(dict(acc['operators'])), 'reason_counts': _json(dict(acc['reason_counts']))})
        latency_rows.append({'family': family, 'p50_ms': family_rows[-1]['rejection_p50_ms'], 'p95_ms': family_rows[-1]['rejection_p95_ms'], 'p99_ms': family_rows[-1]['rejection_p99_ms']})
        for gate in _GATE_ORDER:
            count = int(acc['stage_counts'].get(gate, 0))
            gate_rows.append({'family': family, 'gate': gate, 'count': count, 'rate': count / malicious if malicious else 0.0})
    guard_union: set[str] = set()
    line_union: set[str] = set()
    line_universe: set[str] = set()
    fuzz_by_surface: dict[str, dict[str, Any]] = {}
    fuzz_false_accepts = fuzz_false_rejects = fuzz_crashes = 0
    for row in fuzz:
        surface = row['surface']
        acc = fuzz_by_surface.setdefault(surface, {'executions': 0, 'guards': set(), 'lines': set(), 'universe': set(), 'false_accepts': 0, 'false_rejects': 0, 'crashes': 0, 'corpus': 0, 'signatures': 0})
        acc['executions'] += int(row['attack_scenarios'])
        acc['guards'].update(row['guard_coverage'])
        acc['lines'].update(row.get('covered_line_points', []))
        acc['universe'].update(row.get('line_universe', []))
        acc['false_accepts'] += int(row['false_accepts'])
        acc['false_rejects'] += int(row['false_rejects'])
        acc['crashes'] += int(row['crashes'])
        acc['corpus'] = max(acc['corpus'], int(row['corpus_size']))
        acc['signatures'] += int(row['coverage_signatures'])
        guard_union.update(row['guard_coverage'])
        line_union.update(row.get('covered_line_points', []))
        line_universe.update(row.get('line_universe', []))
        fuzz_false_accepts += int(row['false_accepts'])
        fuzz_false_rejects += int(row['false_rejects'])
        fuzz_crashes += int(row['crashes'])
        case_rows.append({'case_id': row['case_id'], 'kind': row['kind'], 'family': '', 'surface': surface, 'ablation': '', 'context': row['context']['name'], 'mode': row['context']['mode'], 'anchor_mode': row['context']['anchor_mode'], 'replicate': row['replicate'], 'attack_scenarios': row['attack_scenarios'], 'malicious_responses': row['malicious_responses'], 'honest_controls': row['honest_controls'], 'false_accepts': row['false_accepts'], 'false_rejects': row['false_rejects'], 'crashes': row['crashes'], 'stage_mismatches': '', 'rejection_p50_ms': '', 'rejection_p99_ms': '', 'runtime_s': row['runtime_ns'] / 1000000000.0})
    coverage_rows: list[dict[str, Any]] = []
    for surface, acc in sorted(fuzz_by_surface.items()):
        coverage_rows.append({'surface': surface, 'executions': acc['executions'], 'semantic_guards_covered': len(acc['guards']), 'semantic_guards_total': len(GUARD_IDS), 'semantic_guard_coverage': len(acc['guards']) / len(GUARD_IDS), 'sampled_lines_covered': len(acc['lines']), 'sampled_line_universe': len(acc['universe']), 'sampled_line_coverage': len(acc['lines']) / len(acc['universe']) if acc['universe'] else 0.0, 'maximum_corpus_size': acc['corpus'], 'coverage_signatures': acc['signatures'], 'false_accepts': acc['false_accepts'], 'false_rejects': acc['false_rejects'], 'crashes': acc['crashes']})
    ablation_acc: dict[str, dict[str, int]] = {}
    for row in ablations:
        ablation = row['ablation']
        acc = ablation_acc.setdefault(ablation, {'scenarios': 0, 'malicious': 0, 'safe_accepts': 0, 'unsafe_accepts': 0, 'honest_false_rejects': 0, 'crashes': 0})
        acc['scenarios'] += int(row['attack_scenarios'])
        acc['malicious'] += int(row['malicious_responses'])
        acc['safe_accepts'] += int(row['safe_accepts'])
        acc['unsafe_accepts'] += int(row['unsafe_accepts'])
        acc['honest_false_rejects'] += int(row['honest_false_rejects'])
        acc['crashes'] += int(row['crashes'])
        case_rows.append({'case_id': row['case_id'], 'kind': row['kind'], 'family': '', 'surface': '', 'ablation': ablation, 'context': row['context']['name'], 'mode': row['context']['mode'], 'anchor_mode': row['context']['anchor_mode'], 'replicate': row['replicate'], 'attack_scenarios': row['attack_scenarios'], 'malicious_responses': row['malicious_responses'], 'honest_controls': row['honest_controls'], 'false_accepts': row['safe_accepts'], 'false_rejects': row['honest_false_rejects'], 'crashes': row['crashes'], 'stage_mismatches': '', 'rejection_p50_ms': percentile(row['safe_latencies_ns'], 0.5) / 1000000.0, 'rejection_p99_ms': percentile(row['safe_latencies_ns'], 0.99) / 1000000.0, 'runtime_s': row['runtime_ns'] / 1000000000.0})
    ablation_rows = [{'ablation': name, **values, 'unsafe_accept_rate': values['unsafe_accepts'] / values['malicious'] if values['malicious'] else 0.0, 'safe_accept_rate': values['safe_accepts'] / values['malicious'] if values['malicious'] else 0.0} for name, values in sorted(ablation_acc.items())]
    all_false_accepts += fuzz_false_accepts
    all_false_rejects += fuzz_false_rejects
    all_crashes += fuzz_crashes
    h7a = all_false_accepts == 0 and all_false_rejects == 0 and (all_crashes == 0)
    h7b = all_stage_mismatches == 0 and all((int(row['malicious_responses']) > 0 for row in family_rows))
    h7c = len(ablation_rows) == 3 and all((int(row['safe_accepts']) == 0 and int(row['unsafe_accepts']) == int(row['malicious']) and (int(row['crashes']) == 0) for row in ablation_rows))
    hypothesis_rows = [{'hypothesis': 'H7a', 'status': 'PASS' if h7a else 'FAIL', 'criterion': 'zero false accepts, false rejects, and crashes', 'value': f'FA={all_false_accepts};FR={all_false_rejects};Crash={all_crashes}', 'threshold': 'all zero'}, {'hypothesis': 'H7b', 'status': 'PASS' if h7b else 'FAIL', 'criterion': 'all deterministic attacks rejected at the preregistered earliest gate', 'value': all_stage_mismatches, 'threshold': 0}, {'hypothesis': 'H7c', 'status': 'PASS' if h7c else 'FAIL', 'criterion': 'NoCtx, NoAnchor, and NoMeta accept their causal exploit while safe verification rejects it', 'value': _json(ablation_rows), 'threshold': 'safe=0; unsafe=100%'}]
    write_csv(output / 'e7_case_summary.csv', case_rows)
    write_csv(output / 'e7_attack_family_summary.csv', family_rows)
    write_csv(output / 'e7_attack_by_gate.csv', gate_rows)
    write_csv(output / 'e7_rejection_latency.csv', latency_rows)
    write_csv(output / 'e7_fuzz_coverage.csv', coverage_rows)
    write_csv(output / 'e7_ablation_results.csv', ablation_rows)
    categories = list(ATTACK_FAMILIES)
    stacks = [(gate, [next((float(row['count']) for row in gate_rows if row['family'] == family and row['gate'] == gate), 0.0) for family in categories]) for gate in _GATE_ORDER if gate not in {'Accept', 'Crash'}]
    stacked_bar(output / 'fig_E7_attack_by_gate.svg', title='E7 Attack Rejections by Earliest Verification Gate', categories=categories, stacks=stacks, y_label='Malicious responses')
    bar_chart(output / 'fig_E7_rejection_latency.svg', title='E7 Rejection-Latency Distribution by Attack Family', categories=categories, series=[('p50', [float(row['rejection_p50_ms']) for row in family_rows]), ('p99', [float(row['rejection_p99_ms']) for row in family_rows])], y_label='Latency (ms)')
    cov_categories = [row['surface'] for row in coverage_rows]
    bar_chart(output / 'fig_E7_fuzz_coverage.svg', title='E7 Greybox Coverage by Mutation Surface', categories=cov_categories, series=[('semantic guards', [float(row['semantic_guard_coverage']) for row in coverage_rows]), ('sampled lines', [float(row['sampled_line_coverage']) for row in coverage_rows])], y_label='Coverage', percent=True)
    e7_summary = {'deterministic_cases': len(deterministic), 'fuzz_cases': len(fuzz), 'ablation_cases': len(ablations), 'attack_scenarios': sum((int(row['attack_scenarios']) for row in deterministic)), 'malicious_responses': sum((int(row['malicious_responses']) for row in deterministic)) + sum((int(row['malicious_responses']) for row in fuzz)), 'paired_honest_controls': sum((int(row['honest_controls']) for row in deterministic + fuzz + ablations)), 'false_accepts': all_false_accepts, 'false_rejects': all_false_rejects, 'crashes': all_crashes, 'stage_mismatches': all_stage_mismatches, 'zero_event_95pct_upper_bound': upper_zero_event_bound(sum((int(row['malicious_responses']) for row in deterministic + fuzz))) if all_false_accepts == 0 else None, 'semantic_guards_covered': len(guard_union), 'semantic_guards_total': len(GUARD_IDS), 'sampled_lines_covered': len(line_union), 'sampled_line_universe': len(line_universe), 'H7a': 'PASS' if h7a else 'FAIL', 'H7b': 'PASS' if h7b else 'FAIL', 'H7c': 'PASS' if h7c else 'FAIL', 'publication_protocol': {'each_family_at_least_100k_scenarios': all((int(plan_summary['e7_scenarios_by_family'].get(family, 0)) >= 100000 for family in ATTACK_FAMILIES)), 'at_least_one_million_deterministic_attack_scenarios': int(plan_summary['e7_attack_scenarios']) >= 1000000, 'every_malicious_response_has_honest_control': all((int(row['paired_honest_controls']) == int(row['malicious_responses']) for row in family_rows))}}
    return (e7_summary, hypothesis_rows)

def _e8_analysis(results: list[dict[str, Any]], output: Path, config: dict[str, Any], plan_summary: dict[str, Any], complete: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    all_false_accepts = 0
    all_queries = 0
    all_checks = True
    for result in results:
        p = result['parameters']
        m = result['metrics']
        all_false_accepts += int(m['false_accepts'])
        all_queries += int(m['queries'])
        all_checks = all_checks and all((bool(v) for v in result['checks'].values()))
        rows.append({'case_id': result['case_id'], 'n': p['epoch_length'], 'mode': p['mode'], 'variant': p['variant'], 'failure_probability': p['failure_probability'], 'outage_model': p['outage_model'], 'deadline_ms': p['deadline_ms'], 'retry_policy': p['retry_policy'], 'replicate': p['replicate'], 'queries': m['queries'], 'completed': m['completed'], 'safe_rejections': m['safe_rejections'], 'timeouts': m['timeouts'], 'false_accepts': m['false_accepts'], 'completion_rate': m['completion_rate'], 'C_avail': m['C_avail'], 'successful_latency_p50_ms': _finite(m['successful_latency_p50_ms']), 'successful_latency_p95_ms': _finite(m['successful_latency_p95_ms']), 'successful_latency_p99_ms': _finite(m['successful_latency_p99_ms']), 'time_to_reject_p50_ms': _finite(m['time_to_reject_p50_ms']), 'time_to_reject_p95_ms': _finite(m['time_to_reject_p95_ms']), 'incomplete_bytes_p50': _finite(m['incomplete_bytes_p50']), 'incomplete_bytes_p95': _finite(m['incomplete_bytes_p95']), 'incomplete_bytes_total': m['incomplete_bytes_total'], 'total_bytes_consumed': m['total_bytes_consumed'], 'retry_attempts': m['retry_attempts'], 'expected_independent_completion': m['expected_independent_completion'], 'observed_minus_independent': m['observed_minus_independent'], 'service_count': m['service_count'], 'frame_bytes': result['wire']['frame_bytes'], 'verify_median_ms': result['verification_audit']['median_ms'], 'hash_calls_per_accept': result['verification_audit']['hash_calls_per_accept'], 'service_response_bytes': _json(result['wire']['service_response_bytes']), 'service_outages': _json(m['service_outages']), 'service_deadline_misses': _json(m['service_deadline_misses'])})
    write_csv(output / 'e8_case_results.csv', rows)
    keys = ('n', 'mode', 'variant', 'failure_probability', 'outage_model', 'deadline_ms', 'retry_policy')
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        grouped[tuple((row[key] for key in keys))].append(row)
    summaries: list[dict[str, Any]] = []
    for index, (key, values) in enumerate(sorted(grouped.items(), key=lambda item: tuple((str(x) for x in item[0])))):
        base = dict(zip(keys, key))
        completions = [float(row['completion_rate']) for row in values]
        low, high = bootstrap_median_ci(completions, seed=900000 + index, samples=500)

        def med(field: str) -> float | str:
            vals = [float(row[field]) for row in values if row[field] != '']
            return median(vals) if vals else ''
        summaries.append({**base, 'replicates': len(values), 'queries_total': sum((int(row['queries']) for row in values)), 'completion_rate_median': median(completions), 'completion_rate_ci_low': low, 'completion_rate_ci_high': high, 'C_avail_median': 1.0 - median(completions), 'successful_latency_p50_ms_median': med('successful_latency_p50_ms'), 'successful_latency_p95_ms_median': med('successful_latency_p95_ms'), 'successful_latency_p99_ms_median': med('successful_latency_p99_ms'), 'time_to_reject_p50_ms_median': med('time_to_reject_p50_ms'), 'time_to_reject_p95_ms_median': med('time_to_reject_p95_ms'), 'incomplete_bytes_p50_median': med('incomplete_bytes_p50'), 'incomplete_bytes_p95_median': med('incomplete_bytes_p95'), 'expected_independent_completion_median': median([float(row['expected_independent_completion']) for row in values]), 'observed_minus_independent_median': median([float(row['observed_minus_independent']) for row in values]), 'frame_bytes_median': median([float(row['frame_bytes']) for row in values]), 'verify_median_ms': median([float(row['verify_median_ms']) for row in values]), 'false_accepts': sum((int(row['false_accepts']) for row in values))})
    write_csv(output / 'e8_configuration_summary.csv', summaries)
    write_csv(output / 'e8_independence_comparison.csv', [{'n': row['n'], 'variant': row['variant'], 'failure_probability': row['failure_probability'], 'outage_model': row['outage_model'], 'deadline_ms': row['deadline_ms'], 'retry_policy': row['retry_policy'], 'observed_completion': row['completion_rate_median'], 'independent_product': row['expected_independent_completion_median'], 'difference': row['observed_minus_independent_median']} for row in summaries])
    monotonic_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in summaries:
        key = (row['n'], row['variant'], row['outage_model'], row['deadline_ms'], row['retry_policy'])
        monotonic_groups[key].append(row)
    monotonic_violations: list[dict[str, Any]] = []
    for key, values in monotonic_groups.items():
        ordered = sorted(values, key=lambda row: float(row['failure_probability']))
        for previous, current in zip(ordered, ordered[1:]):
            if float(current['completion_rate_median']) > float(previous['completion_rate_median']) + 0.02:
                monotonic_violations.append({'group': _json(key), 'previous_p': previous['failure_probability'], 'previous_completion': previous['completion_rate_median'], 'current_p': current['failure_probability'], 'current_completion': current['completion_rate_median']})
    write_csv(output / 'e8_monotonicity_violations.csv', monotonic_violations, fieldnames=('group', 'previous_p', 'previous_completion', 'current_p', 'current_completion'))
    lookup = {(row['n'], row['variant'], row['failure_probability'], row['outage_model'], row['deadline_ms'], row['retry_policy']): row for row in summaries}
    topology_rows: list[dict[str, Any]] = []
    ext_not_worse_count = ext_comparisons = 0
    for key, ext in lookup.items():
        n, variant, p, model, deadline, retry = key
        if variant != 'ext_split' or float(p) <= 0:
            continue
        full = lookup.get((n, 'full_colocated', p, model, deadline, retry))
        if full is None:
            continue
        ext_comparisons += 1
        ext_rate = float(ext['completion_rate_median'])
        full_rate = float(full['completion_rate_median'])
        passed = ext_rate <= full_rate + 0.02
        ext_not_worse_count += int(passed)
        topology_rows.append({'n': n, 'failure_probability': p, 'outage_model': model, 'deadline_ms': deadline, 'retry_policy': retry, 'full_colocated_completion': full_rate, 'ext_split_completion': ext_rate, 'difference_ext_minus_full': ext_rate - full_rate, 'extra_dependency_observed': passed})
    write_csv(output / 'e8_topology_comparison.csv', topology_rows)
    retry_rows: list[dict[str, Any]] = []
    for row in summaries:
        if row['retry_policy'] != 'no_retry':
            continue
        other = lookup.get((row['n'], row['variant'], row['failure_probability'], row['outage_model'], row['deadline_ms'], 'one_retry'))
        if other:
            retry_rows.append({'n': row['n'], 'variant': row['variant'], 'failure_probability': row['failure_probability'], 'outage_model': row['outage_model'], 'deadline_ms': row['deadline_ms'], 'no_retry_completion': row['completion_rate_median'], 'one_retry_completion': other['completion_rate_median'], 'difference': float(other['completion_rate_median']) - float(row['completion_rate_median'])})
    write_csv(output / 'e8_retry_sensitivity.csv', retry_rows)
    profile = config['e8']['catalog_profile']
    catalog_rows: list[dict[str, Any]] = []
    n_values = [int(x) for x in config['e8']['epoch_lengths']]
    for n in n_values:
        for mode in ('full', 'leaf', 'ext'):
            variant = profile['variant_by_mode'][mode]
            row = lookup.get((n, variant, float(profile['failure_probability']), profile['outage_model'], float(profile['deadline_ms']), profile['retry_policy']))
            if row is None:
                continue
            catalog_rows.append({'n': n, 'mode': mode, 'success_probability': row['completion_rate_median'], 'C_avail': 1.0 - float(row['completion_rate_median']), 'variant': variant, 'failure_probability': profile['failure_probability'], 'outage_model': profile['outage_model'], 'deadline_ms': profile['deadline_ms'], 'retry_policy': profile['retry_policy'], 'replicates': row['replicates'], 'queries_total': row['queries_total'], 'source': f"E8:{plan_summary['label']}:{variant}:p={profile['failure_probability']}:deadline={profile['deadline_ms']}", 'publication_eligible': bool(plan_summary['publication_eligible'] and complete and all_checks)})
    write_csv(output / 'availability_catalog.csv', catalog_rows)
    expected_catalog_rows = len(n_values) * 3
    h8a = all_false_accepts == 0 and all_checks and (not monotonic_violations)
    h8b = ext_comparisons > 0 and ext_not_worse_count == ext_comparisons
    h8c = len(catalog_rows) == expected_catalog_rows and all((0.0 <= float(row['success_probability']) <= 1.0 for row in catalog_rows))
    hypothesis_rows = [{'hypothesis': 'H8a', 'status': 'PASS' if h8a else 'FAIL', 'criterion': 'withholding never causes acceptance and completion is non-increasing with failure probability', 'value': f'FA={all_false_accepts};monotonic_violations={len(monotonic_violations)}', 'threshold': 'FA=0; violations=0'}, {'hypothesis': 'H8b', 'status': 'PASS' if h8b else 'FAIL', 'criterion': 'Ext split topology exposes the expected extra witness-source dependency', 'value': f'matched={ext_comparisons};passed={ext_not_worse_count}', 'threshold': 'all matched comparisons'}, {'hypothesis': 'H8c', 'status': 'PASS' if h8c else 'FAIL', 'criterion': 'complete empirical availability catalogue for E9', 'value': len(catalog_rows), 'threshold': expected_catalog_rows}]
    available_n = sorted({int(row['n']) for row in summaries})
    plot_n = 512 if 512 in available_n else available_n[0]
    available_deadlines = sorted({float(row['deadline_ms']) for row in summaries})
    plot_deadline = 500.0 if 500.0 in available_deadlines else available_deadlines[-1]
    variants = ['full_colocated', 'full_split', 'leaf_colocated', 'leaf_split', 'ext_split']
    line_series: list[tuple[str, list[tuple[float, float]]]] = []
    for variant in variants:
        points = [(float(row['failure_probability']) * 100.0, float(row['completion_rate_median'])) for row in summaries if int(row['n']) == plot_n and row['variant'] == variant and (row['outage_model'] == 'independent') and (float(row['deadline_ms']) == plot_deadline) and (row['retry_policy'] == 'no_retry')]
        if points:
            line_series.append((variant, points))
    line_chart(output / 'fig_E8_completion_vs_failure.svg', title=f'E8 Completion under Independent Withholding (n={plot_n}, deadline={plot_deadline:g} ms)', x_label='Per-service failure probability (%)', y_label='Completion probability', series=line_series, y_min=0.0, y_max=1.02)
    reject_series: list[tuple[str, list[tuple[float, float]]]] = []
    p_candidates = sorted({float(row['failure_probability']) for row in summaries if float(row['failure_probability']) > 0})
    plot_p = 0.25 if 0.25 in p_candidates else p_candidates[len(p_candidates) // 2]
    for variant in ('full_colocated', 'ext_split'):
        points = [(float(row['deadline_ms']), float(row['time_to_reject_p50_ms_median'])) for row in summaries if int(row['n']) == plot_n and row['variant'] == variant and (row['outage_model'] == 'correlated') and (float(row['failure_probability']) == plot_p) and (row['retry_policy'] == 'no_retry') and (row['time_to_reject_p50_ms_median'] != '')]
        if points:
            reject_series.append((variant, points))
    line_chart(output / 'fig_E8_time_to_reject.svg', title=f'E8 Safe-Rejection Time under Correlated Withholding (p={plot_p:g})', x_label='Deadline (ms)', y_label='Median time to safe rejection (ms)', series=reject_series)
    topology_series: list[tuple[str, list[tuple[float, float]]]] = []
    for variant in ('full_colocated', 'full_split', 'ext_split'):
        points = [(float(row['failure_probability']) * 100.0, float(row['completion_rate_median'])) for row in summaries if int(row['n']) == plot_n and row['variant'] == variant and (row['outage_model'] == 'independent') and (float(row['deadline_ms']) == plot_deadline) and (row['retry_policy'] == 'no_retry')]
        if points:
            topology_series.append((variant, points))
    line_chart(output / 'fig_E8_topology_comparison.svg', title=f'E8 Co-located versus Split Retrieval Topologies (n={plot_n})', x_label='Per-service failure probability (%)', y_label='Completion probability', series=topology_series, y_min=0.0, y_max=1.02)
    retry_series: list[tuple[str, list[tuple[float, float]]]] = []
    for model in ('independent', 'correlated'):
        for retry in ('no_retry', 'one_retry'):
            points = [(float(row['failure_probability']) * 100.0, float(row['completion_rate_median'])) for row in summaries if int(row['n']) == plot_n and row['variant'] == 'ext_split' and (row['outage_model'] == model) and (float(row['deadline_ms']) == plot_deadline) and (row['retry_policy'] == retry)]
            if points:
                retry_series.append((f'{model}/{retry}', points))
    line_chart(output / 'fig_E8_retry_and_correlation.svg', title=f'E8 Retry Sensitivity and Correlated Outages (Ext, n={plot_n})', x_label='Per-service failure probability (%)', y_label='Completion probability', series=retry_series, y_min=0.0, y_max=1.02)
    e8_summary = {'cases': len(results), 'simulated_queries': all_queries, 'false_accepts': all_false_accepts, 'all_case_checks_pass': all_checks, 'monotonicity_violations': len(monotonic_violations), 'ext_dependency_comparisons': ext_comparisons, 'ext_dependency_comparisons_passed': ext_not_worse_count, 'availability_catalog_rows': len(catalog_rows), 'availability_catalog_expected_rows': expected_catalog_rows, 'H8a': 'PASS' if h8a else 'FAIL', 'H8b': 'PASS' if h8b else 'FAIL', 'H8c': 'PASS' if h8c else 'FAIL', 'publication_protocol': {'failure_probabilities': sorted({float(row['failure_probability']) for row in rows}), 'deadlines_ms': sorted({float(row['deadline_ms']) for row in rows}), 'outage_models': sorted({row['outage_model'] for row in rows}), 'retry_policies': sorted({row['retry_policy'] for row in rows}), 'variants': sorted({row['variant'] for row in rows}), 'correct_object_or_bottom_only': all_checks}}
    return (e8_summary, hypothesis_rows)

def analyze(plan_dir: Path, raw_e7: Path, raw_e8: Path, results_dir: Path, *, allow_incomplete: bool=False) -> dict[str, Any]:
    plan_summary = load_json(plan_dir / 'plan_summary.json')
    config = load_json(plan_dir / 'resolved_config.json')
    e7_audit = inspect_blocks(plan_dir, raw_e7, 'E7')
    e8_audit = inspect_blocks(plan_dir, raw_e8, 'E8')
    complete = bool(e7_audit['complete'] and e8_audit['complete'])
    if not complete and (not allow_incomplete):
        raise RuntimeError(f"incomplete raw results: E7 missing={e7_audit['missing_indices']} corrupt={e7_audit['corrupt_blocks']}; E8 missing={e8_audit['missing_indices']} corrupt={e8_audit['corrupt_blocks']}")
    results_dir.mkdir(parents=True, exist_ok=True)
    e7_results = _flatten_results(e7_audit['payloads'])
    e8_results = _flatten_results(e8_audit['payloads'])
    e7_summary, e7_hyp = _e7_analysis(e7_results, results_dir, plan_summary)
    e8_summary, e8_hyp = _e8_analysis(e8_results, results_dir, config, plan_summary, complete)
    hypothesis_rows = e7_hyp + e8_hyp
    write_csv(results_dir / 'hypothesis_checks.csv', hypothesis_rows)
    plan_audit = {'complete': complete, 'e7_expected_blocks': e7_audit['expected_blocks'], 'e7_valid_blocks': e7_audit['valid_blocks'], 'e7_missing_indices': e7_audit['missing_indices'], 'e7_corrupt_blocks': e7_audit['corrupt_blocks'], 'e7_extra_files': e7_audit['extra_files'], 'e8_expected_blocks': e8_audit['expected_blocks'], 'e8_valid_blocks': e8_audit['valid_blocks'], 'e8_missing_indices': e8_audit['missing_indices'], 'e8_corrupt_blocks': e8_audit['corrupt_blocks'], 'e8_extra_files': e8_audit['extra_files'], 'expected_e7_cases': plan_summary['e7_cases'], 'actual_e7_cases': len(e7_results), 'expected_e8_cases': plan_summary['e8_cases'], 'actual_e8_cases': len(e8_results)}
    internal_pass = complete and len(e7_results) == int(plan_summary['e7_cases']) and (len(e8_results) == int(plan_summary['e8_cases']))
    all_hyp = all((row['status'] == 'PASS' for row in hypothesis_rows))
    publication_protocol = {'publication_eligible_configuration': bool(plan_summary['publication_eligible']), 'e7_each_family_at_least_100k_scenarios': bool(e7_summary['publication_protocol']['each_family_at_least_100k_scenarios']), 'e7_at_least_one_million_scenarios': bool(e7_summary['publication_protocol']['at_least_one_million_deterministic_attack_scenarios']), 'e7_paired_controls': bool(e7_summary['publication_protocol']['every_malicious_response_has_honest_control']), 'e8_failure_probability_set_complete': set(e8_summary['publication_protocol']['failure_probabilities']) >= {0.0, 0.01, 0.05, 0.1, 0.25, 0.5}, 'e8_deadline_set_complete': set(e8_summary['publication_protocol']['deadlines_ms']) >= {100.0, 500.0, 2000.0}, 'e8_independent_and_correlated': set(e8_summary['publication_protocol']['outage_models']) >= {'independent', 'correlated'}, 'e8_no_retry_and_one_retry': set(e8_summary['publication_protocol']['retry_policies']) >= {'no_retry', 'one_retry'}}
    summary = {'schema_version': 1, 'experiment': 'E7+E8', 'label': plan_summary['label'], 'status': 'PASS' if internal_pass else 'FAIL', 'plan_audit': plan_audit, 'all_case_integrity_checks_pass': internal_pass, 'all_primary_hypotheses_pass': all_hyp, 'hypotheses': {row['hypothesis']: row['status'] for row in hypothesis_rows}, 'e7': e7_summary, 'e8': e8_summary, 'publication_protocol': publication_protocol, 'notes': ['E7 contains only malformed, tampered, stale, replayed, or equivocated responses; withholding is isolated in E8.', 'E8 returns either the exact correct object or bottom; it never corrupts objects.', 'E8 uses deterministic virtual time for outage/deadline trials and audits every fixture with the real cryptographic verifier.', 'Hypothesis failure is a scientific result and does not by itself invalidate a complete run.']}
    atomic_write_json(results_dir / 'summary.json', summary)
    report_lines = ['# Experiment 7 — Adversarial Integrity and Availability Results', '', f"Run label: `{plan_summary['label']}`  ", f"Execution status: **{summary['status']}**  ", f"Primary hypotheses: **{('PASS' if all_hyp else 'ONE OR MORE FAILED')}**", '', '## E7 adversarial integrity', '', f"The run evaluated {e7_summary['attack_scenarios']:,} deterministic attack scenarios and {e7_summary['malicious_responses']:,} malicious responses including greybox mutations. It observed {e7_summary['false_accepts']} false accepts, {e7_summary['false_rejects']} false rejects, {e7_summary['crashes']} crashes, and {e7_summary['stage_mismatches']} preregistered-gate mismatches.", '', f"With zero false accepts, the one-sided 95% upper bound on the malicious-response acceptance probability is {e7_summary['zero_event_95pct_upper_bound']:.3e}." if e7_summary['zero_event_95pct_upper_bound'] is not None else 'False accepts were observed; no zero-event upper bound is reported.', '', f"H7a: **{e7_summary['H7a']}**; H7b: **{e7_summary['H7b']}**; H7c: **{e7_summary['H7c']}**.", '', '## E8 availability and withholding', '', f"The run evaluated {e8_summary['cases']:,} availability configurations and {e8_summary['simulated_queries']:,} virtual-time queries. Every successful fixture was audited by the real SHA-256/Ed25519 verifier. The fail-closed state machine observed {e8_summary['false_accepts']} false accepts under withholding.", '', f"H8a: **{e8_summary['H8a']}**; H8b: **{e8_summary['H8b']}**; H8c: **{e8_summary['H8c']}**.", '', f"The E9-compatible availability catalogue contains {e8_summary['availability_catalog_rows']} rows (expected {e8_summary['availability_catalog_expected_rows']}).", '', '## Interpretation rule', '', '`status=PASS` means that the plan was complete, all result files were structurally valid, and all internal invariants held. A hypothesis marked `FAIL` must remain in the paper as an empirical result; it must not be removed by changing thresholds after observing the data.']
    atomic_write_text(results_dir / 'report.md', '\n'.join(report_lines) + '\n')
    write_manifest(results_dir)
    return summary

def validate_results(results_dir: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    summary_path = results_dir / 'summary.json'
    if not summary_path.exists():
        errors.append('summary.json is missing')
    else:
        try:
            summary = load_json(summary_path)
            if summary.get('status') != 'PASS':
                errors.append(f"summary status is {summary.get('status')}")
            if not summary.get('plan_audit', {}).get('complete'):
                errors.append('plan audit is incomplete')
            if not summary.get('all_case_integrity_checks_pass'):
                errors.append('case integrity checks failed')
        except Exception as exc:
            errors.append(f'summary decode failed: {type(exc).__name__}: {exc}')
    manifest_ok, manifest_errors = validate_manifest(results_dir)
    if not manifest_ok:
        errors.extend(manifest_errors)
    return (not errors, errors)
