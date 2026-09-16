from __future__ import annotations
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from .accumulator import aggregate_anchor_bytes, aggregate_evidence_bytes_for_power_of_two, checkpoint_object_bytes, direct_anchor_bytes, direct_evidence_bytes
from .benchmark import case_result_path
from .svg import line_chart
from .util import atomic_write_json, atomic_write_text, bootstrap_ci, csv_write, flatten_dict, jsonl_iter, linear_fit, read_json_gz, relative_range, sha256_file, stable_seed

def inspect_raw(plan_path: str | Path, raw_dir: str | Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    results: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    missing_indices: list[int] = []
    corrupt: list[dict[str, Any]] = []
    expected = 0
    for index, case in enumerate(jsonl_iter(plan_path)):
        expected += 1
        path = case_result_path(raw_dir, str(case['case_id']))
        if not path.is_file():
            missing_indices.append(index)
            continue
        try:
            result = read_json_gz(path)
            if result.get('experiment') != 'E5':
                raise ValueError('experiment identifier mismatch')
            result_case = result.get('case', {})
            if result_case.get('case_id') != case['case_id']:
                raise ValueError('case identifier mismatch')
            if not result.get('checks', {}).get('all_internal_checks_pass', False):
                raise ValueError('internal case checks did not pass')
            results.append((index, case, result))
        except Exception as exc:
            corrupt.append({'index': index, 'case_id': case['case_id'], 'path': str(path), 'error': f'{type(exc).__name__}: {exc}'})
    return {'schema_version': 1, 'experiment': 'E5', 'expected_cases': expected, 'valid_cases': len(results), 'missing_indices': missing_indices, 'corrupt_cases': corrupt, 'complete': not missing_indices and (not corrupt) and (len(results) == expected), 'results': results}

def missing_case_indices(plan_path: str | Path, raw_dir: str | Path) -> list[int]:
    audit = inspect_raw(plan_path, raw_dir)
    indices = list(audit['missing_indices'])
    indices.extend((int(row['index']) for row in audit['corrupt_cases']))
    return sorted(set(indices))

def _number(value: Any) -> float:
    if value is None:
        return float('nan')
    return float(value)

def _median_finite(values: Iterable[Any]) -> float:
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(finite) if finite else float('nan')

def _query_launch_row(result: dict[str, Any]) -> dict[str, Any]:
    case = result['case']
    return {'case_id': case['case_id'], 'config_id': case['config_id'], 'trial': case['trial'], 'anchor_mode': case['anchor_mode'], 'prefix_count': case['prefix_count'], 'depth': int(case['prefix_count']).bit_length() - 1, 'measured_queries': case['measured_queries'], 'evidence_bytes': result['storage']['evidence_bytes']['median'], 'expected_evidence_bytes': result['storage']['expected_evidence_bytes'], 'anchor_bytes': result['storage']['verifier_resident_anchor_bytes'], 'external_certificate_bytes': result['storage']['external_certificate_bytes_at_prefix'], 'generation_median_ns': result['latency']['evidence_generation_ns']['median'], 'generation_p95_ns': result['latency']['evidence_generation_ns']['p95'], 'verify_median_ns': result['latency']['verify_anchor_ns']['median'], 'verify_p95_ns': result['latency']['verify_anchor_ns']['p95'], 'sha256_per_query': result['counts']['sha256_operations_per_query'], 'all_internal_checks_pass': result['checks']['all_internal_checks_pass'], 'hostname': result.get('host', {}).get('hostname'), 'cpu_model': result.get('host', {}).get('cpu_model')}

def _update_launch_row(result: dict[str, Any]) -> dict[str, Any]:
    case = result['case']
    return {'case_id': case['case_id'], 'config_id': case['config_id'], 'trial': case['trial'], 'anchor_mode': case['anchor_mode'], 'target_prefix': case['target_prefix'], 'checkpoint_interval': case['checkpoint_interval'], 'measured_updates': case['measured_updates'], 'statements_per_update': result['counts']['statements_per_update'], 'checkpoints_at_prefix': result['counts']['checkpoint_objects_at_target_prefix'], 'anchor_bytes': result['storage']['verifier_resident_anchor_bytes'], 'external_certificate_bytes': result['storage']['external_certificate_bytes_at_prefix'], 'checkpoint_object_bytes': result['storage']['checkpoint_object_bytes'], 'update_median_ns': result['latency']['checkpoint_update_ns']['median'], 'update_p95_ns': result['latency']['checkpoint_update_ns']['p95'], 'amortized_median_ns_per_epoch': result['latency']['amortized_update_ns_per_epoch']['median'], 'amortized_p95_ns_per_epoch': result['latency']['amortized_update_ns_per_epoch']['p95'], 'sha256_per_update': result['crypto']['sha256_operations_per_update']['median'], 'all_internal_checks_pass': result['checks']['all_internal_checks_pass']}

def _pruning_launch_row(result: dict[str, Any]) -> dict[str, Any]:
    case = result['case']
    return {'case_id': case['case_id'], 'config_id': case['config_id'], 'trial': case['trial'], 'trial_kind': case['trial_kind'], 'policy': case['policy'], 'anchor_mode': case['anchor_mode'], 'checkpoint_interval': case['checkpoint_interval'], 'arrival_rate_eps': case['arrival_rate_eps'], 'certificate_delay_ms': case['certificate_delay_ms'], 'hot_window_epochs': case['hot_window_epochs'], 'fault': case['fault'], 'sweep_tags': json.dumps(case.get('sweep_tags', []), separators=(',', ':')), 'total_epochs': result['counts']['total_epochs'], 'pruned_epochs': result['counts']['pruned_epochs'], 'pending_epochs': result['counts']['pending_epochs'], 'fault_injections': result['counts']['fault_injections'], 'fault_rejections': result['counts']['fault_rejections'], 'anchor_attempts': result['counts']['anchor_attempts'], 'anchor_rejects': result['counts']['anchor_rejects'], 'checkpoint_count': result['counts']['checkpoint_count'], 'checkpoint_dropped': result['counts']['checkpoint_dropped'], 'gate_violations': result['safety']['total_gate_violations'], 'pruned_before_finality': result['safety']['pruned_before_finality'], 'pruned_without_root_match': result['safety']['pruned_without_local_root_match'], 'pruned_without_valid_anchor': result['safety']['pruned_without_valid_anchor'], 'max_retained_epochs': result['backlog']['max_retained_epochs'], 'final_retained_epochs': result['backlog']['final_retained_epochs'], 'max_retained_payload_bytes': result['backlog']['max_retained_payload_bytes'], 'max_hot_window_overflow_epochs': result['backlog']['max_hot_window_overflow_epochs'], 'backlog_area_epoch_ms': result['backlog']['backlog_area_epoch_ms'], 'anchor_bytes': result['storage']['verifier_resident_anchor_bytes'], 'proof_bytes_median': result['storage']['proof_bytes']['median'], 'verify_anchor_p95_ns': result['latency']['verify_anchor_ns']['p95'], 'time_to_prune_p50_ms': result['latency']['time_to_prune_from_finality_ms']['median'], 'time_to_prune_p95_ms': result['latency']['time_to_prune_from_finality_ms']['p95'], 'pending_duration_p95_ms': result['latency']['pending_duration_ms']['p95'], 'all_internal_checks_pass': result['checks']['all_internal_checks_pass']}

def _group_rows(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row['config_id'])].append(row)
    if kind == 'query':
        metrics = ['evidence_bytes', 'expected_evidence_bytes', 'anchor_bytes', 'external_certificate_bytes', 'generation_median_ns', 'generation_p95_ns', 'verify_median_ns', 'verify_p95_ns', 'sha256_per_query']
    elif kind == 'update':
        metrics = ['anchor_bytes', 'external_certificate_bytes', 'checkpoint_object_bytes', 'update_median_ns', 'update_p95_ns', 'amortized_median_ns_per_epoch', 'amortized_p95_ns_per_epoch', 'sha256_per_update', 'checkpoints_at_prefix']
    elif kind == 'pruning':
        metrics = ['pruned_epochs', 'pending_epochs', 'fault_injections', 'fault_rejections', 'anchor_attempts', 'anchor_rejects', 'checkpoint_count', 'checkpoint_dropped', 'gate_violations', 'pruned_before_finality', 'pruned_without_root_match', 'pruned_without_valid_anchor', 'max_retained_epochs', 'final_retained_epochs', 'max_retained_payload_bytes', 'max_hot_window_overflow_epochs', 'backlog_area_epoch_ms', 'anchor_bytes', 'proof_bytes_median', 'verify_anchor_p95_ns', 'time_to_prune_p50_ms', 'time_to_prune_p95_ms', 'pending_duration_p95_ms']
    else:
        raise ValueError(f'unknown row-group kind: {kind}')
    output: list[dict[str, Any]] = []
    per_launch_only = {'case_id', 'trial', 'hostname', 'cpu_model', 'all_internal_checks_pass'}
    excluded = per_launch_only | set(metrics)
    for config_id, launches in sorted(groups.items()):
        first = launches[0]
        common = {key: value for key, value in first.items() if key not in excluded}
        grouped: dict[str, Any] = {'config_id': config_id, 'launches': len(launches), **common}
        for metric in metrics:
            values = [launch.get(metric) for launch in launches]
            finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
            grouped[metric] = statistics.median(finite) if finite else None
            if metric in {'generation_median_ns', 'verify_median_ns', 'update_median_ns', 'amortized_median_ns_per_epoch', 'max_retained_epochs', 'time_to_prune_p95_ms'} and finite:
                lower, upper = bootstrap_ci(finite, stable_seed('E5', config_id, metric), iterations=1000)
                grouped[f'{metric}_ci95_low'] = lower
                grouped[f'{metric}_ci95_high'] = upper
        grouped['all_internal_checks_pass'] = all((bool(launch['all_internal_checks_pass']) for launch in launches))
        output.append(grouped)
    return output

def _check(rows: list[dict[str, Any]], hypothesis: str, name: str, passed: bool | None, observed: Any, criterion: str, primary: bool=True) -> None:
    status = 'NOT_EVALUATED' if passed is None else 'PASS' if passed else 'FAIL'
    rows.append({'hypothesis': hypothesis, 'check': name, 'status': status, 'primary': primary, 'observed': json.dumps(observed, sort_keys=True, separators=(',', ':')) if isinstance(observed, (dict, list, tuple)) else observed, 'criterion': criterion})

def _nondecreasing(values: list[float], tolerance: float=1e-09) -> bool:
    return all((next_value + tolerance >= value for value, next_value in zip(values, values[1:])))

def _has_sweep_tag(row: dict[str, Any], tag: str) -> bool:
    value = row.get('sweep_tags', [])
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    return tag in value

def _write_manifest(output_dir: Path) -> None:
    lines = []
    for path in sorted(output_dir.rglob('*')):
        if not path.is_file() or path.name == 'manifest.sha256':
            continue
        lines.append(f'{sha256_file(path)}  {path.relative_to(output_dir).as_posix()}')
    atomic_write_text(output_dir / 'manifest.sha256', '\n'.join(lines) + '\n')

def _write_figures(output_dir: Path, query_configs: list[dict[str, Any]], update_configs: list[dict[str, Any]], pruning_configs: list[dict[str, Any]]) -> None:
    evidence_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    verify_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in query_configs:
        mode = str(row['anchor_mode'])
        evidence_series[mode].append((float(row['prefix_count']), float(row['evidence_bytes'])))
        verify_series[mode].append((float(row['prefix_count']), float(row['verify_p95_ns']) / 1000000.0))
    line_chart(output_dir / 'fig_E5_anchor_evidence_bytes.svg', title='Direct and aggregate anchor evidence size', x_label='Certified prefix length k* (statements)', y_label='Evidence bytes per query', series=evidence_series, x_log2=True)
    line_chart(output_dir / 'fig_E5_verify_anchor_latency.svg', title='VerifyAnchor p95 latency', x_label='Certified prefix length k* (statements)', y_label='p95 latency (ms)', series=verify_series, x_log2=True)
    maximum_prefix = max((int(row['target_prefix']) for row in update_configs), default=0)
    update_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in update_configs:
        if int(row['target_prefix']) != maximum_prefix:
            continue
        label = str(row['anchor_mode'])
        update_series[label].append((float(row['checkpoint_interval']), float(row['amortized_median_ns_per_epoch']) / 1000.0))
    line_chart(output_dir / 'fig_E5_checkpoint_amortization.svg', title=f'Checkpoint-update amortisation at k*={maximum_prefix}', x_label='Checkpoint interval κ (epochs)', y_label='Median update cost per epoch (µs)', series=update_series, x_log2=True)
    baseline = [row for row in pruning_configs if row.get('policy') == 'safe' and row.get('fault') == 'none']
    delay_groups: dict[tuple[str, int, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in baseline:
        delay_groups[str(row['anchor_mode']), int(row['checkpoint_interval']), float(row['arrival_rate_eps']), int(row['hot_window_epochs'])].append(row)
    ranked_delay_groups = sorted(delay_groups.items(), key=lambda item: (-len({float(row['certificate_delay_ms']) for row in item[1]}), item[0]))
    backlog_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    prune_time_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for (mode, interval, rate, hot), rows in ranked_delay_groups:
        unique_delays = {float(row['certificate_delay_ms']) for row in rows}
        if len(unique_delays) < 2:
            continue
        label = f'{mode} κ={interval}, λ={rate:g}/s'
        for row in rows:
            backlog_series[label].append((float(row['certificate_delay_ms']), float(row['max_retained_epochs'])))
            if row.get('time_to_prune_p95_ms') is not None:
                prune_time_series[label].append((float(row['certificate_delay_ms']), float(row['time_to_prune_p95_ms'])))
        if len(backlog_series) >= 6:
            break
    line_chart(output_dir / 'fig_E5_backlog_vs_delay.svg', title='Safe-pruning backlog under certificate delay', x_label='Certificate delay (ms)', y_label='Maximum retained epochs', series=backlog_series)
    line_chart(output_dir / 'fig_E5_time_to_prune.svg', title='Time to safe pruning under certificate delay', x_label='Certificate delay (ms)', y_label='p95 time from finality to pruning (ms)', series=prune_time_series)
    interval_groups: dict[tuple[float, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in baseline:
        if row.get('anchor_mode') != 'aggregate':
            continue
        interval_groups[float(row['arrival_rate_eps']), float(row['certificate_delay_ms']), int(row['hot_window_epochs'])].append(row)
    ranked_interval_groups = sorted(interval_groups.items(), key=lambda item: (-len({int(row['checkpoint_interval']) for row in item[1]}), item[0]))
    interval_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for (rate, delay, _hot), rows in ranked_interval_groups[:4]:
        if len({int(row['checkpoint_interval']) for row in rows}) < 2:
            continue
        label = f'λ={rate:g}/s, delay={delay:g} ms'
        interval_series[label] = [(float(row['checkpoint_interval']), float(row['time_to_prune_p95_ms'] or 0.0)) for row in rows]
    line_chart(output_dir / 'fig_E5_checkpoint_interval_tradeoff.svg', title='Checkpoint interval versus safe-pruning delay', x_label='Checkpoint interval κ (epochs)', y_label='p95 time from finality to pruning (ms)', series=interval_series, x_log2=True)

def analyze(plan_path: str | Path, raw_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = inspect_raw(plan_path, raw_dir)
    if not audit['complete']:
        raise RuntimeError(f"raw results are incomplete: missing={audit['missing_indices']} corrupt={audit['corrupt_cases']}")
    query_launches: list[dict[str, Any]] = []
    update_launches: list[dict[str, Any]] = []
    pruning_launches: list[dict[str, Any]] = []
    all_case_checks = True
    for _, _, result in audit['results']:
        all_case_checks = all_case_checks and bool(result['checks']['all_internal_checks_pass'])
        if result['trial_kind'] == 'anchor_query':
            query_launches.append(_query_launch_row(result))
        elif result['trial_kind'] == 'checkpoint_update':
            update_launches.append(_update_launch_row(result))
        elif result['trial_kind'] in {'pruning', 'unsafe_counterexample'}:
            pruning_launches.append(_pruning_launch_row(result))
        else:
            raise RuntimeError(f"unknown result kind: {result['trial_kind']}")
    query_configs = _group_rows(query_launches, 'query')
    update_configs = _group_rows(update_launches, 'update')
    pruning_configs = _group_rows(pruning_launches, 'pruning')
    csv_write(output_dir / 'anchor_query_launches.csv', query_launches)
    csv_write(output_dir / 'anchor_query_summary.csv', query_configs)
    csv_write(output_dir / 'checkpoint_update_launches.csv', update_launches)
    csv_write(output_dir / 'checkpoint_update_summary.csv', update_configs)
    csv_write(output_dir / 'pruning_case_summary.csv', pruning_launches)
    csv_write(output_dir / 'pruning_config_summary.csv', pruning_configs)
    prefixes = sorted({int(row['prefix_count']) for row in query_configs})
    object_rows = [{'object': 'direct_verifier_anchor', 'prefix_count': None, 'bytes': direct_anchor_bytes(), 'formula': 'mode/version + shard + Ed25519 public key'}, {'object': 'aggregate_verifier_anchor', 'prefix_count': None, 'bytes': aggregate_anchor_bytes(), 'formula': 'mode/version + shard + k* + accumulator root + signature + public key'}, {'object': 'direct_query_evidence', 'prefix_count': None, 'bytes': direct_evidence_bytes(), 'formula': 'typed RootStmt + Ed25519 signature + envelope'}, {'object': 'aggregate_checkpoint_object', 'prefix_count': None, 'bytes': checkpoint_object_bytes(), 'formula': 'shard + k* + accumulator root + Ed25519 signature + envelope'}]
    object_rows.extend(({'object': 'aggregate_query_evidence', 'prefix_count': prefix, 'bytes': aggregate_evidence_bytes_for_power_of_two(prefix), 'formula': 'typed RootStmt + position-bound logarithmic sibling path + envelope'} for prefix in prefixes))
    csv_write(output_dir / 'object_sizes.csv', object_rows)
    model_rows: list[dict[str, Any]] = []
    for mode in ('direct', 'aggregate'):
        rows = sorted((row for row in query_configs if row['anchor_mode'] == mode), key=lambda row: int(row['prefix_count']))
        xs = [math.log2(float(row['prefix_count'])) for row in rows]
        for metric in ('evidence_bytes', 'generation_median_ns', 'verify_median_ns'):
            ys = [float(row[metric]) for row in rows]
            fit = linear_fit(xs, ys)
            model_rows.append({'model': f'{mode}_{metric}_vs_log2_prefix', 'x': 'log2(prefix_count)', 'y': metric, **fit})
    maximum_prefix = max((int(row['target_prefix']) for row in update_configs), default=0)
    aggregate_update = sorted((row for row in update_configs if row['anchor_mode'] == 'aggregate' and int(row['target_prefix']) == maximum_prefix), key=lambda row: int(row['checkpoint_interval']))
    if len(aggregate_update) >= 2:
        fit = linear_fit([1.0 / float(row['checkpoint_interval']) for row in aggregate_update], [float(row['amortized_median_ns_per_epoch']) for row in aggregate_update])
        model_rows.append({'model': 'aggregate_amortized_update_vs_inverse_kappa', 'x': '1/checkpoint_interval', 'y': 'amortized_median_ns_per_epoch', **fit})
    csv_write(output_dir / 'model_fits.csv', model_rows)
    hypothesis_rows: list[dict[str, Any]] = []
    direct_query = [row for row in query_configs if row['anchor_mode'] == 'direct']
    aggregate_query = [row for row in query_configs if row['anchor_mode'] == 'aggregate']
    direct_constant = {int(round(float(row['evidence_bytes']))) for row in direct_query}
    _check(hypothesis_rows, 'H5a', 'direct evidence is constant-size across certified prefixes', bool(direct_query) and direct_constant == {direct_evidence_bytes()}, sorted(direct_constant), f'all direct evidence objects equal {direct_evidence_bytes()} bytes')
    aggregate_formula_observed = {str(int(row['prefix_count'])): int(round(float(row['evidence_bytes']))) for row in aggregate_query}
    aggregate_formula_ok = bool(aggregate_query) and all((int(round(float(row['evidence_bytes']))) == aggregate_evidence_bytes_for_power_of_two(int(row['prefix_count'])) for row in aggregate_query))
    _check(hypothesis_rows, 'H5a', 'aggregate evidence follows the exact logarithmic serialization formula', aggregate_formula_ok, aggregate_formula_observed, 'observed bytes equal typed RootStmt plus a position-bound O(log k*) proof')
    anchor_sizes_ok = bool(query_configs) and all((int(round(float(row['anchor_bytes']))) == (direct_anchor_bytes() if row['anchor_mode'] == 'direct' else aggregate_anchor_bytes()) for row in query_configs))
    _check(hypothesis_rows, 'H5a', 'verifier-resident anchor state is constant-size', anchor_sizes_ok, {'direct': direct_anchor_bytes(), 'aggregate': aggregate_anchor_bytes()}, 'serialized verifier anchor bytes are independent of historical prefix length')
    operation_counts_ok = bool(query_configs) and all((int(round(float(row['sha256_per_query']))) == (0 if row['anchor_mode'] == 'direct' else int(row['depth']) + 2) for row in query_configs))
    _check(hypothesis_rows, 'H5a', 'verification hash work matches constant/direct and logarithmic/aggregate formulas', operation_counts_ok, {f"{row['anchor_mode']}@{int(row['prefix_count'])}": int(round(float(row['sha256_per_query']))) for row in query_configs}, 'direct uses zero accumulator hashes; aggregate uses log2(k*) + 2 SHA-256 operations')
    query_checks_ok = all((bool(row['all_internal_checks_pass']) for row in query_launches))
    _check(hypothesis_rows, 'H5a', 'all anchor-query cryptographic invariants pass', bool(query_launches) and query_checks_ok, {'launches': len(query_launches)}, 'all valid evidence accepted; single-bit tampering rejected; exact size/hash counts')
    aggregate_evidence_fit = next((row for row in model_rows if row['model'] == 'aggregate_evidence_bytes_vs_log2_prefix'), None)
    _check(hypothesis_rows, 'H5a', 'aggregate evidence has a positive log2(k*) trend', None if aggregate_evidence_fit is None else float(aggregate_evidence_fit['slope']) > 0, aggregate_evidence_fit or {}, 'positive fitted slope; exact serialization equality is the primary asymptotic test', primary=False)
    aggregate_generation_fit = next((row for row in model_rows if row['model'] == 'aggregate_generation_median_ns_vs_log2_prefix'), None)
    aggregate_verify_fit = next((row for row in model_rows if row['model'] == 'aggregate_verify_median_ns_vs_log2_prefix'), None)
    _check(hypothesis_rows, 'H5a', 'aggregate proof generation has a positive depth component', None if aggregate_generation_fit is None else float(aggregate_generation_fit['slope']) > 0, aggregate_generation_fit or {}, 'positive slope versus log2(k*)', primary=False)
    _check(hypothesis_rows, 'H5a', 'aggregate VerifyAnchor has a non-negative depth component', None if aggregate_verify_fit is None else float(aggregate_verify_fit['slope']) >= 0, aggregate_verify_fit or {}, 'non-negative slope versus log2(k*)', primary=False)
    safe_rows = [row for row in pruning_launches if row['policy'] == 'safe']
    safe_violations = sum((int(round(float(row['gate_violations']))) for row in safe_rows))
    _check(hypothesis_rows, 'H5b', 'safe Commit-and-Prune never bypasses finality, root equality, or valid anchoring', bool(safe_rows) and safe_violations == 0, {'safe_cases': len(safe_rows), 'gate_violations': safe_violations}, 'zero prunes before finality, without local root recomputation success, or without valid anchor evidence')
    grouped_faults: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in safe_rows:
        if row['fault'] != 'none':
            grouped_faults[str(row['anchor_mode']), str(row['fault'])].append(row)
    fault_matrix: list[dict[str, Any]] = []
    all_faults_fail_closed = bool(grouped_faults)
    for (mode, fault), rows in sorted(grouped_faults.items()):
        injections = sum((int(round(float(row['fault_injections']))) for row in rows))
        rejections = sum((int(round(float(row['fault_rejections']))) for row in rows))
        pending = sum((int(round(float(row['pending_epochs']))) for row in rows))
        dropped = sum((int(round(float(row['checkpoint_dropped']))) for row in rows))
        violations = sum((int(round(float(row['gate_violations']))) for row in rows))
        max_backlog = max((float(row['max_retained_epochs']) for row in rows))
        if fault in {'wrong_position', 'stale_prefix_root', 'invalid_signature', 'uncovered_prefix'}:
            behaviour_observed = injections > 0 and rejections > 0
        elif fault == 'missing_certificate':
            behaviour_observed = injections > 0 and (pending > 0 or dropped > 0 or max_backlog > 0)
        else:
            behaviour_observed = injections > 0 and pending > 0
        fail_closed = behaviour_observed and violations == 0
        all_faults_fail_closed = all_faults_fail_closed and fail_closed
        fault_matrix.append({'anchor_mode': mode, 'fault': fault, 'launches': len(rows), 'fault_injections': injections, 'fault_rejections': rejections, 'pending_epochs': pending, 'checkpoint_dropped': dropped, 'max_retained_epochs': max_backlog, 'gate_violations': violations, 'fail_closed': fail_closed})
    csv_write(output_dir / 'fault_matrix.csv', fault_matrix)
    _check(hypothesis_rows, 'H5b', 'all declared certification and pruning faults are exercised and fail closed', all_faults_fail_closed, fault_matrix, 'every fault is injected, produces rejection or retained hot state as applicable, and causes zero unsafe prunes')
    baseline_configs = [row for row in pruning_configs if row.get('policy') == 'safe' and row.get('fault') == 'none']
    delay_groups: dict[tuple[str, int, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in baseline_configs:
        delay_groups[str(row['anchor_mode']), int(row['checkpoint_interval']), float(row['arrival_rate_eps']), int(row['hot_window_epochs'])].append(row)
    delay_checks: list[dict[str, Any]] = []
    for key, rows in sorted(delay_groups.items()):
        by_delay: dict[float, dict[str, Any]] = {}
        for row in rows:
            by_delay[float(row['certificate_delay_ms'])] = row
        ordered = [by_delay[value] for value in sorted(by_delay)]
        if len(ordered) < 2:
            continue
        backlogs = [float(row['max_retained_epochs']) for row in ordered]
        prune_times = [float(row['time_to_prune_p95_ms']) if row.get('time_to_prune_p95_ms') is not None else float('nan') for row in ordered]
        backlog_ok = _nondecreasing(backlogs, tolerance=1e-06)
        finite_prune = [value for value in prune_times if math.isfinite(value)]
        prune_ok = len(finite_prune) < 2 or _nondecreasing(finite_prune, tolerance=1e-06)
        delay_checks.append({'anchor_mode': key[0], 'checkpoint_interval': key[1], 'arrival_rate_eps': key[2], 'hot_window_epochs': key[3], 'delays_ms': json.dumps(sorted(by_delay)), 'max_retained_epochs': json.dumps(backlogs), 'time_to_prune_p95_ms': json.dumps(prune_times), 'backlog_non_decreasing': backlog_ok, 'prune_time_non_decreasing': prune_ok})
    csv_write(output_dir / 'delay_backlog_checks.csv', delay_checks)
    delay_pass = None if not delay_checks else all((bool(row['backlog_non_decreasing']) and bool(row['prune_time_non_decreasing']) for row in delay_checks))
    _check(hypothesis_rows, 'H5b', 'certificate delay never reduces the retained-hot-state burden', delay_pass, delay_checks, 'within fixed mode, κ, arrival rate, and hot window, maximum backlog and p95 time-to-prune are non-decreasing with delay')
    unsafe_rows = [row for row in pruning_launches if row['policy'] == 'unsafe_delete_after_upload']
    unsafe_violations = sum((int(round(float(row['gate_violations']))) for row in unsafe_rows))
    _check(hypothesis_rows, 'H5b', 'labelled delete-after-upload counterexample violates the gate', bool(unsafe_rows) and unsafe_violations > 0, {'counterexample_cases': len(unsafe_rows), 'gate_violations': unsafe_violations}, 'at least one violation is observed; counterexample is excluded from safe-policy aggregates', primary=False)
    update_checks_ok = all((bool(row['all_internal_checks_pass']) for row in update_launches))
    _check(hypothesis_rows, 'H5c', 'checkpoint updates reproduce the certified target root', bool(update_launches) and update_checks_ok, {'launches': len(update_launches)}, 'all target-root, signature-length, sample-count, and hash-count checks pass')
    reduction_observations: list[dict[str, Any]] = []
    certificate_reduction_ok = True
    for prefix in sorted({int(row['target_prefix']) for row in update_configs}):
        direct = next((row for row in update_configs if row['anchor_mode'] == 'direct' and int(row['target_prefix']) == prefix), None)
        if direct is None:
            continue
        for aggregate in (row for row in update_configs if row['anchor_mode'] == 'aggregate' and int(row['target_prefix']) == prefix and (int(row['checkpoint_interval']) > 1)):
            reduced = float(aggregate['external_certificate_bytes']) < float(direct['external_certificate_bytes'])
            certificate_reduction_ok = certificate_reduction_ok and reduced
            reduction_observations.append({'prefix': prefix, 'kappa': int(aggregate['checkpoint_interval']), 'direct_bytes': float(direct['external_certificate_bytes']), 'aggregate_bytes': float(aggregate['external_certificate_bytes']), 'reduced': reduced})
    _check(hypothesis_rows, 'H5c', 'aggregate checkpoints reduce certificate-object storage for κ > 1', bool(reduction_observations) and certificate_reduction_ok, reduction_observations, 'aggregate checkpoint bytes at every tested κ > 1 are lower than direct per-epoch certificate bytes')
    amortization_checks: list[dict[str, Any]] = []
    for prefix in sorted({int(row['target_prefix']) for row in update_configs}):
        rows = sorted((row for row in update_configs if row['anchor_mode'] == 'aggregate' and int(row['target_prefix']) == prefix), key=lambda row: int(row['checkpoint_interval']))
        if len(rows) < 2:
            continue
        smallest, largest = (rows[0], rows[-1])
        small_cost = float(smallest['amortized_median_ns_per_epoch'])
        large_cost = float(largest['amortized_median_ns_per_epoch'])
        endpoint_reduction = large_cost < small_cost
        amortization_checks.append({'target_prefix': prefix, 'smallest_kappa': int(smallest['checkpoint_interval']), 'largest_kappa': int(largest['checkpoint_interval']), 'smallest_kappa_cost_ns_per_epoch': small_cost, 'largest_kappa_cost_ns_per_epoch': large_cost, 'endpoint_reduction': endpoint_reduction, 'reduction_factor': small_cost / large_cost if large_cost else None})
    amortization_pass = None if not amortization_checks else all((bool(row['endpoint_reduction']) for row in amortization_checks))
    _check(hypothesis_rows, 'H5c', 'larger checkpoints amortise update and signature overhead', amortization_pass, amortization_checks, 'at each tested prefix, the largest κ has lower median update cost per epoch than the smallest κ')
    interval_groups: dict[tuple[float, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in baseline_configs:
        if row['anchor_mode'] != 'aggregate':
            continue
        interval_groups[float(row['arrival_rate_eps']), float(row['certificate_delay_ms']), int(row['hot_window_epochs'])].append(row)
    checkpoint_tradeoffs: list[dict[str, Any]] = []
    for key, rows in sorted(interval_groups.items()):
        by_interval: dict[int, dict[str, Any]] = {}
        for row in rows:
            by_interval[int(row['checkpoint_interval'])] = row
        ordered = [by_interval[value] for value in sorted(by_interval)]
        if len(ordered) < 2:
            continue
        smallest, largest = (ordered[0], ordered[-1])
        small_prune = float(smallest['time_to_prune_p95_ms'] or 0.0)
        large_prune = float(largest['time_to_prune_p95_ms'] or 0.0)
        small_backlog = float(smallest['max_retained_epochs'])
        large_backlog = float(largest['max_retained_epochs'])
        tradeoff_observed = large_prune >= small_prune and large_backlog >= small_backlog
        checkpoint_tradeoffs.append({'arrival_rate_eps': key[0], 'certificate_delay_ms': key[1], 'hot_window_epochs': key[2], 'smallest_kappa': int(smallest['checkpoint_interval']), 'largest_kappa': int(largest['checkpoint_interval']), 'smallest_kappa_time_to_prune_p95_ms': small_prune, 'largest_kappa_time_to_prune_p95_ms': large_prune, 'smallest_kappa_max_retained_epochs': small_backlog, 'largest_kappa_max_retained_epochs': large_backlog, 'tradeoff_observed': tradeoff_observed})
    csv_write(output_dir / 'checkpoint_tradeoff.csv', checkpoint_tradeoffs)
    checkpoint_tradeoff_pass = None if not checkpoint_tradeoffs else all((bool(row['tradeoff_observed']) for row in checkpoint_tradeoffs))
    _check(hypothesis_rows, 'H5c', 'larger κ increases the time-to-prune or retained-hot-state burden', checkpoint_tradeoff_pass, checkpoint_tradeoffs, 'under fixed arrival rate, delay, and hot window, the largest κ has no lower p95 prune time and no lower maximum backlog than the smallest κ')
    csv_write(output_dir / 'hypothesis_checks.csv', hypothesis_rows)
    _write_figures(output_dir, query_configs, update_configs, pruning_configs)
    primary_checks = [row for row in hypothesis_rows if row['primary']]
    hypothesis_status: dict[str, bool | None] = {}
    for hypothesis in sorted({row['hypothesis'] for row in primary_checks}):
        statuses = [row['status'] for row in primary_checks if row['hypothesis'] == hypothesis]
        if 'FAIL' in statuses:
            hypothesis_status[hypothesis] = False
        elif 'PASS' in statuses:
            hypothesis_status[hypothesis] = True
        else:
            hypothesis_status[hypothesis] = None
    all_primary_hypotheses_pass = bool(hypothesis_status) and all((value is True for value in hypothesis_status.values()))
    summary = {'schema_version': 1, 'experiment': 'E5', 'status': 'PASS' if audit['complete'] and all_case_checks else 'FAIL', 'plan_audit': {'complete': audit['complete'], 'expected_cases': audit['expected_cases'], 'valid_cases': audit['valid_cases'], 'missing_indices': audit['missing_indices'], 'corrupt_cases': audit['corrupt_cases']}, 'case_counts': {'anchor_query': len(query_launches), 'checkpoint_update': len(update_launches), 'safe_pruning': len(safe_rows), 'unsafe_counterexample': len(unsafe_rows)}, 'configuration_counts': {'anchor_query': len(query_configs), 'checkpoint_update': len(update_configs), 'pruning': len(pruning_configs)}, 'all_case_integrity_checks_pass': all_case_checks, 'all_primary_hypotheses_pass': all_primary_hypotheses_pass, 'hypotheses': hypothesis_status, 'safety_totals': {'safe_gate_violations': safe_violations, 'unsafe_counterexample_gate_violations': unsafe_violations}, 'object_sizes': {'direct_anchor_bytes': direct_anchor_bytes(), 'aggregate_anchor_bytes': aggregate_anchor_bytes(), 'direct_evidence_bytes': direct_evidence_bytes(), 'aggregate_checkpoint_object_bytes': checkpoint_object_bytes()}}
    atomic_write_json(output_dir / 'summary.json', summary)
    report_lines = ['# Experiment E5 — Certified Root Anchoring and Safe Pruning', '', f"- Raw-plan completeness: **{audit['valid_cases']}/{audit['expected_cases']} cases**.", f"- Case-level integrity checks: **{('PASS' if all_case_checks else 'FAIL')}**.", f"- Primary hypotheses: **{('PASS' if all_primary_hypotheses_pass else 'NOT ALL PASSED')}**.", f'- Safe-policy gate violations: **{safe_violations}**.', f'- Labelled unsafe-counterexample violations: **{unsafe_violations}**.', '', '## Anchor objects', '', f'Direct verifier anchor: {direct_anchor_bytes()} B; aggregate verifier anchor: {aggregate_anchor_bytes()} B; direct per-query evidence: {direct_evidence_bytes()} B.', 'Aggregate evidence includes the typed RootStmt and a position-bound logarithmic Merkle-accumulator proof.', '', '## Hypothesis checks', '']
    for row in hypothesis_rows:
        report_lines.append(f"- **{row['hypothesis']} / {row['check']}**: {row['status']} — {row['criterion']}.")
    report_lines.extend(['', '## Interpretation boundary', '', 'The unsafe delete-after-upload policy is a deliberately labelled counterexample and is excluded from safe-policy performance aggregates. Logical certificate delays drive the backlog simulation; cryptographic root recomputation, proof generation, signing, and verification are executed and timed on the host CPU.', ''])
    atomic_write_text(output_dir / 'report.md', '\n'.join(report_lines))
    _write_manifest(output_dir)
    return summary

def validate_results(results_dir: str | Path) -> dict[str, Any]:
    results_dir = Path(results_dir)
    required = ['summary.json', 'report.md', 'anchor_query_summary.csv', 'checkpoint_update_summary.csv', 'pruning_config_summary.csv', 'fault_matrix.csv', 'delay_backlog_checks.csv', 'checkpoint_tradeoff.csv', 'hypothesis_checks.csv', 'manifest.sha256']
    missing = [name for name in required if not (results_dir / name).is_file()]
    errors: list[str] = []
    if missing:
        errors.append(f'missing required files: {missing}')
    summary: dict[str, Any] = {}
    if not missing:
        try:
            summary = json.loads((results_dir / 'summary.json').read_text(encoding='utf-8'))
            if summary.get('status') != 'PASS':
                errors.append(f"summary status is {summary.get('status')!r}")
            if not summary.get('plan_audit', {}).get('complete'):
                errors.append('plan audit is incomplete')
            if not summary.get('all_case_integrity_checks_pass'):
                errors.append('case integrity checks failed')
        except Exception as exc:
            errors.append(f'summary parse failed: {type(exc).__name__}: {exc}')
    manifest_path = results_dir / 'manifest.sha256'
    manifest_checked = 0
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            expected_hash, relative = line.split('  ', 1)
            path = results_dir / relative
            if not path.is_file():
                errors.append(f'manifest path is missing: {relative}')
                continue
            actual_hash = sha256_file(path)
            if actual_hash != expected_hash:
                errors.append(f'manifest hash mismatch: {relative}')
            manifest_checked += 1
    return {'status': 'PASS' if not errors else 'FAIL', 'results_dir': str(results_dir), 'manifest_files_checked': manifest_checked, 'all_primary_hypotheses_pass': summary.get('all_primary_hypotheses_pass'), 'errors': errors}
