from __future__ import annotations
import collections
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Sequence
from .svg import line_chart
from .util import atomic_write_json, atomic_write_text, bootstrap_ci, csv_write, jsonl_iter, read_json, read_json_gz, relative_range, sha256_file, stable_seed
NS_PER_MS = 1000000.0
NS_PER_US = 1000.0

def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and (not math.isfinite(value)):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value

def _safe_float(value: Any, default: float=math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result

def _median(values: Iterable[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(clean) if clean else math.nan

def _mean(values: Iterable[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.fmean(clean) if clean else math.nan

def _ratio(maximum: float, minimum: float) -> float:
    if not math.isfinite(maximum) or not math.isfinite(minimum):
        return math.nan
    if minimum <= 0:
        return 1.0 if maximum == minimum else math.inf
    return maximum / minimum

def _plan_rows(plan_path: str | Path) -> list[dict[str, Any]]:
    return list(jsonl_iter(plan_path))

def inspect_raw(plan_path: str | Path, raw_dir: str | Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    expected = _plan_rows(plan_path)
    expected_ids = {row['case_id'] for row in expected}
    valid: list[dict[str, Any]] = []
    missing: list[str] = []
    corrupt: list[dict[str, str]] = []
    for case in expected:
        path = raw_dir / f"{case['case_id']}.json.gz"
        if not path.exists():
            missing.append(case['case_id'])
            continue
        try:
            result = read_json_gz(path)
            if result.get('experiment') != 'E4':
                raise ValueError('wrong experiment identifier')
            if result.get('case', {}).get('case_id') != case['case_id']:
                raise ValueError('case identifier mismatch')
            valid.append(result)
        except Exception as exc:
            corrupt.append({'case_id': case['case_id'], 'error': f'{type(exc).__name__}: {exc}'})
    extra = sorted((path.stem.removesuffix('.json') for path in raw_dir.glob('*.json.gz') if path.name.removesuffix('.json.gz') not in expected_ids))
    return {'expected_cases': len(expected), 'valid_cases': len(valid), 'missing_case_ids': missing, 'corrupt_cases': corrupt, 'extra_case_ids': extra, 'complete': not missing and (not corrupt) and (len(valid) == len(expected)), 'results': valid}

def missing_case_indices(plan_path: str | Path, raw_dir: str | Path) -> list[int]:
    rows = _plan_rows(plan_path)
    raw_dir = Path(raw_dir)
    missing: list[int] = []
    for index, case in enumerate(rows):
        path = raw_dir / f"{case['case_id']}.json.gz"
        try:
            result = read_json_gz(path)
            checks = result.get('checks', {})
            if result.get('case', {}).get('case_id') != case['case_id']:
                raise ValueError('case mismatch')
            if not checks.get('all_queries_accepted') or not checks.get('hash_calls_exact'):
                raise ValueError('case integrity checks failed')
        except Exception:
            missing.append(index)
    return missing

def _step_row(result: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    case = result['case']
    latency = step['latency']
    capacity = _safe_float(result.get('capacity_calibration', {}).get('capacity_qps'), 0.0)
    target = _safe_float(step.get('target_rate_qps'), 0.0)
    offered = int(step.get('offered_attempts', 0))
    accepted = int(step.get('accepted_within_window', 0))
    submitted = int(step.get('submitted', 0))
    completion = accepted / offered if offered else math.nan
    admission = submitted / offered if offered else math.nan
    phase_names = ('check_ns', 'decode_ns', 'resolve_ns', 'member_ns', 'anchor_ns')
    crypto_median_ns = sum((_safe_float(latency[name].get('median'), 0.0) for name in phase_names))
    local_median_ns = crypto_median_ns + _safe_float(latency['lookup_ns'].get('median'), 0.0)
    service = step.get('service', {}) if isinstance(step.get('service'), dict) else {}
    resource = step.get('resource', {}) if isinstance(step.get('resource'), dict) else {}
    tags = list(case.get('sweep_tags', [case.get('primary_sweep', 'unknown')]))
    row: dict[str, Any] = {'case_id': case['case_id'], 'config_id': case['config_id'], 'trial': int(case['trial']), 'primary_sweep': case.get('primary_sweep'), 'sweep_tags': ';'.join(tags), 'step_index': int(step.get('step_index', 0)), 'mode': case['mode'], 'distribution': case['distribution'], 'shard_count': int(case['shard_count']), 'historical_epochs': int(case['historical_epochs']), 'index_records': int(case['shard_count']) * int(case['historical_epochs']), 'index_size_bytes': int(result.get('dataset', {}).get('size_bytes', 0)), 'epoch_length': int(case['epoch_length']), 'merkle_depth': int(case['epoch_length']).bit_length() - 1, 'block_bytes': int(case['block_bytes']), 'concurrent_clients': int(case['concurrent_clients']), 'verifier_workers': int(case['verifier_workers']), 'target_rtt_ms': float(case['target_rtt_ms']), 'bandwidth_mbps': float(case['bandwidth_mbps']), 'capacity_qps': capacity, 'target_rate_qps': target, 'target_fraction_capacity': target / capacity if capacity else math.nan, 'offered_attempts': offered, 'submitted': submitted, 'backpressure': int(step.get('backpressure', 0)), 'accepted_total': int(step.get('accepted_total', 0)), 'accepted_within_window': accepted, 'failed': int(step.get('failed', 0)), 'completion_ratio': completion, 'admission_ratio': admission, 'throughput_qps': _safe_float(step.get('throughput_qps'), 0.0), 'drain_throughput_qps': _safe_float(step.get('drain_throughput_qps'), 0.0), 'latency_p50_ms': _safe_float(latency['e2e_ns'].get('median')) / NS_PER_MS, 'latency_p95_ms': _safe_float(latency['e2e_ns'].get('p95')) / NS_PER_MS, 'latency_p99_ms': _safe_float(latency['e2e_ns'].get('p99')) / NS_PER_MS, 'scheduled_latency_p95_ms': _safe_float(latency['scheduled_e2e_ns'].get('p95')) / NS_PER_MS, 'queue_p50_ms': _safe_float(latency['queue_ns'].get('median')) / NS_PER_MS, 'queue_p95_ms': _safe_float(latency['queue_ns'].get('p95')) / NS_PER_MS, 'lookup_p50_us': _safe_float(latency['lookup_ns'].get('median')) / NS_PER_US, 'lookup_p95_us': _safe_float(latency['lookup_ns'].get('p95')) / NS_PER_US, 'fetch_p50_ms': _safe_float(latency['fetch_ns'].get('median')) / NS_PER_MS, 'check_p50_us': _safe_float(latency['check_ns'].get('median')) / NS_PER_US, 'decode_p50_us': _safe_float(latency['decode_ns'].get('median')) / NS_PER_US, 'resolve_p50_us': _safe_float(latency['resolve_ns'].get('median')) / NS_PER_US, 'member_p50_us': _safe_float(latency['member_ns'].get('median')) / NS_PER_US, 'anchor_p50_us': _safe_float(latency['anchor_ns'].get('median')) / NS_PER_US, 'crypto_pipeline_p50_us': crypto_median_ns / NS_PER_US, 'local_pipeline_p50_us': local_median_ns / NS_PER_US, 'hash_calls_min': _safe_float(step.get('hash_calls', {}).get('min')), 'hash_calls_max': _safe_float(step.get('hash_calls', {}).get('max')), 'jain_service_ratio': _safe_float(step.get('jain_service_ratio')), 'starved_shards': int(step.get('starved_shards', 0)), 'cpu_utilization_pct': _safe_float(resource.get('cpu_utilization_pct_of_allocated')), 'run_queue_mean': _safe_float(resource.get('run_queue_mean')), 'run_queue_max': _safe_float(resource.get('run_queue_max')), 'rss_kib_max': _safe_float(resource.get('rss_kib_max')), 'minor_faults': _safe_float(resource.get('minor_faults')), 'major_faults': _safe_float(resource.get('major_faults')), 'voluntary_context_switches': _safe_float(resource.get('voluntary_context_switches')), 'nonvoluntary_context_switches': _safe_float(resource.get('nonvoluntary_context_switches')), 'system_context_switches': _safe_float(resource.get('system_context_switches')), 'network_rx_bytes': _safe_float(resource.get('network_rx_bytes')), 'network_tx_bytes': _safe_float(resource.get('network_tx_bytes')), 'service_cpu_pct': _safe_float(service.get('cpu_utilization_pct_of_allocated')), 'service_errors': int(service.get('errors', 0)) if not service.get('error') else -1, 'service_processing_p95_us': _safe_float(service.get('processing_latency_ns', {}).get('p95')) / NS_PER_US, 'service_headroom_pass': bool(step.get('service_headroom_pass', False)), 'all_queries_accepted': bool(result.get('checks', {}).get('all_queries_accepted', False)), 'hash_calls_exact': bool(result.get('checks', {}).get('hash_calls_exact', False))}
    return row

def _step_rows(results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        for step in result.get('rate_steps', []):
            rows.append(_step_row(result, step))
    return rows

def _group(rows: Iterable[dict[str, Any]], fields: Sequence[str]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        grouped[tuple((row[field] for field in fields))].append(row)
    return grouped

def _configuration_summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ('config_id', 'step_index', 'mode', 'distribution', 'shard_count', 'historical_epochs', 'concurrent_clients', 'verifier_workers')
    output: list[dict[str, Any]] = []
    for key, items in sorted(_group(rows, fields).items(), key=lambda pair: str(pair[0])):
        first = items[0]
        qps = [float(item['throughput_qps']) for item in items]
        p95 = [float(item['latency_p95_ms']) for item in items]
        qps_low, qps_high = bootstrap_ci(qps, stable_seed(key, 'qps'), iterations=1000)
        p95_low, p95_high = bootstrap_ci(p95, stable_seed(key, 'p95'), iterations=1000)
        output.append({**{field: first[field] for field in fields}, 'primary_sweep': first['primary_sweep'], 'sweep_tags': first['sweep_tags'], 'trials': len(items), 'capacity_qps_median': _median((item['capacity_qps'] for item in items)), 'target_rate_qps_median': _median((item['target_rate_qps'] for item in items)), 'target_fraction_capacity_median': _median((item['target_fraction_capacity'] for item in items)), 'throughput_qps_median': _median(qps), 'throughput_qps_mean': _mean(qps), 'throughput_qps_ci95_low': qps_low, 'throughput_qps_ci95_high': qps_high, 'latency_p50_ms_median': _median((item['latency_p50_ms'] for item in items)), 'latency_p95_ms_median': _median(p95), 'latency_p95_ms_ci95_low': p95_low, 'latency_p95_ms_ci95_high': p95_high, 'latency_p99_ms_median': _median((item['latency_p99_ms'] for item in items)), 'queue_p95_ms_median': _median((item['queue_p95_ms'] for item in items)), 'lookup_p50_us_median': _median((item['lookup_p50_us'] for item in items)), 'lookup_p95_us_median': _median((item['lookup_p95_us'] for item in items)), 'crypto_pipeline_p50_us_median': _median((item['crypto_pipeline_p50_us'] for item in items)), 'local_pipeline_p50_us_median': _median((item['local_pipeline_p50_us'] for item in items)), 'cpu_utilization_pct_mean': _mean((item['cpu_utilization_pct'] for item in items)), 'rss_kib_max_median': _median((item['rss_kib_max'] for item in items)), 'jain_service_ratio_min': min((float(item['jain_service_ratio']) for item in items)), 'starved_shards_total': sum((int(item['starved_shards']) for item in items)), 'completion_ratio_median': _median((item['completion_ratio'] for item in items)), 'service_headroom_fraction': _mean((float(item['service_headroom_pass']) for item in items)), 'all_queries_accepted': all((bool(item['all_queries_accepted']) for item in items)), 'all_hash_calls_exact': all((bool(item['hash_calls_exact']) for item in items))})
    return output

def _saturation_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ramp = [row for row in rows if row['step_index'] >= 0 and bool({'concurrency_ramp', 'core_scaling', 'workload'}.intersection(row['sweep_tags'].split(';')))]
    output: list[dict[str, Any]] = []
    for (config_id, trial), items in sorted(_group(ramp, ('config_id', 'trial')).items()):
        ordered = sorted(items, key=lambda row: row['target_rate_qps'])
        if not ordered:
            continue
        max_qps = max((float(row['throughput_qps']) for row in ordered))
        threshold = 0.95 * max_qps
        knee = next((row for row in ordered if float(row['throughput_qps']) >= threshold), ordered[-1])
        low = ordered[0]
        high = ordered[-1]
        overload = next((row for row in ordered if float(row['completion_ratio']) < 0.95 or float(row['latency_p95_ms']) >= 2.0 * max(float(low['latency_p95_ms']), 1e-09)), high)
        output.append({'config_id': config_id, 'trial': trial, 'primary_sweep': low['primary_sweep'], 'sweep_tags': low['sweep_tags'], 'distribution': low['distribution'], 'concurrent_clients': low['concurrent_clients'], 'verifier_workers': low['verifier_workers'], 'shard_count': low['shard_count'], 'historical_epochs': low['historical_epochs'], 'maximum_throughput_qps': max_qps, 'knee_95pct_target_qps': float(knee['target_rate_qps']), 'knee_95pct_throughput_qps': float(knee['throughput_qps']), 'overload_onset_target_qps': float(overload['target_rate_qps']), 'low_load_p95_ms': float(low['latency_p95_ms']), 'high_load_p95_ms': float(high['latency_p95_ms']), 'high_to_low_p95_ratio': _ratio(float(high['latency_p95_ms']), float(low['latency_p95_ms'])), 'high_load_completion_ratio': float(high['completion_ratio'])})
    return output

def _history_analysis(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subset = [row for row in rows if 'history_axis' in row['sweep_tags'].split(';') and row['mode'] == 'full']
    points: list[dict[str, Any]] = []
    per_distribution: list[dict[str, Any]] = []
    for (distribution,), dist_rows in sorted(_group(subset, ('distribution',)).items()):
        by_history = _group(dist_rows, ('historical_epochs',))
        lookup_values: list[float] = []
        crypto_values: list[float] = []
        for (history,), items in sorted(by_history.items()):
            lookup = _median((item['lookup_p50_us'] for item in items))
            crypto = _median((item['crypto_pipeline_p50_us'] for item in items))
            points.append({'distribution': distribution, 'historical_epochs': history, 'index_records': int(history) * int(items[0]['shard_count']), 'lookup_p50_us_median': lookup, 'lookup_p95_us_median': _median((item['lookup_p95_us'] for item in items)), 'crypto_pipeline_p50_us_median': crypto, 'local_pipeline_p50_us_median': _median((item['local_pipeline_p50_us'] for item in items)), 'major_faults_median': _median((item['major_faults'] for item in items)), 'trials': len(items)})
            lookup_values.append(lookup)
            crypto_values.append(crypto)
        per_distribution.append({'distribution': distribution, 'history_points': len(by_history), 'lookup_max_to_min_ratio': _ratio(max(lookup_values), min(lookup_values)) if lookup_values else math.nan, 'crypto_relative_range': relative_range(crypto_values)})
    evaluated = bool(per_distribution) and all((item['history_points'] >= 3 for item in per_distribution))
    pass_lookup = evaluated and all((item['lookup_max_to_min_ratio'] <= 4.0 for item in per_distribution))
    pass_crypto = evaluated and all((item['crypto_relative_range'] <= 0.3 for item in per_distribution))
    check = {'hypothesis': 'H4a', 'description': 'Indexed MSI lookup and fixed-depth local verification remain largely independent of historical depth.', 'status': 'PASS' if pass_lookup and pass_crypto else 'FAIL' if evaluated else 'NOT_EVALUATED', 'thresholds': {'lookup_max_to_min_ratio': 4.0, 'crypto_relative_range': 0.3}, 'per_distribution': per_distribution}
    return (points, check)

def _core_analysis(rows: Sequence[dict[str, Any]], saturation: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sat = [row for row in saturation if 'core_scaling' in row.get('sweep_tags', '').split(';') and row['distribution'] == 'uniform']
    output: list[dict[str, Any]] = []
    by_workers = _group(sat, ('verifier_workers',))
    for (workers,), items in sorted(by_workers.items()):
        output.append({'verifier_workers': workers, 'trials': len(items), 'maximum_throughput_qps_median': _median((item['maximum_throughput_qps'] for item in items)), 'knee_95pct_target_qps_median': _median((item['knee_95pct_target_qps'] for item in items)), 'high_to_low_p95_ratio_median': _median((item['high_to_low_p95_ratio'] for item in items)), 'high_load_completion_ratio_median': _median((item['high_load_completion_ratio'] for item in items))})
    evaluated = len(output) >= 2
    if evaluated:
        ordered = sorted(output, key=lambda item: item['verifier_workers'])
        speedup = _ratio(float(ordered[-1]['maximum_throughput_qps_median']), float(ordered[0]['maximum_throughput_qps_median']))
        maximum_workers = int(ordered[-1]['verifier_workers'])
        minimum_speedup = max(1.5, 0.45 * maximum_workers)
        monotonic = all((float(current['maximum_throughput_qps_median']) >= 0.85 * float(previous['maximum_throughput_qps_median']) for previous, current in zip(ordered, ordered[1:])))
        tail_ratios = [float(item['high_to_low_p95_ratio_median']) for item in ordered]
        tail_rise_fraction = sum((value >= 1.25 for value in tail_ratios)) / len(tail_ratios)
        passed = speedup >= minimum_speedup and monotonic and (tail_rise_fraction >= 0.5)
    else:
        speedup = minimum_speedup = tail_rise_fraction = math.nan
        monotonic = False
        passed = False
    check = {'hypothesis': 'H4b', 'description': 'Throughput increases with verifier cores until saturation, after which tail latency rises.', 'status': 'PASS' if passed else 'FAIL' if evaluated else 'NOT_EVALUATED', 'metrics': {'maximum_core_speedup': speedup, 'minimum_required_speedup': minimum_speedup, 'throughput_monotonic_with_15pct_tolerance': monotonic, 'fraction_of_core_levels_with_p95_rise_ge_1.25': tail_rise_fraction}}
    return (output, check)

def _shard_analysis(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subset = [row for row in rows if 'shard_axis' in row['sweep_tags'].split(';') and row['mode'] == 'full']
    points: list[dict[str, Any]] = []
    per_distribution: list[dict[str, Any]] = []
    for (distribution,), dist_rows in sorted(_group(subset, ('distribution',)).items()):
        by_shards = _group(dist_rows, ('shard_count',))
        crypto_values: list[float] = []
        hash_exact = True
        for (shards,), items in sorted(by_shards.items()):
            crypto = _median((item['crypto_pipeline_p50_us'] for item in items))
            points.append({'distribution': distribution, 'shard_count': shards, 'crypto_pipeline_p50_us_median': crypto, 'lookup_p50_us_median': _median((item['lookup_p50_us'] for item in items)), 'throughput_qps_median': _median((item['throughput_qps'] for item in items)), 'latency_p95_ms_median': _median((item['latency_p95_ms'] for item in items)), 'jain_service_ratio_min': min((float(item['jain_service_ratio']) for item in items)), 'starved_shards_total': sum((int(item['starved_shards']) for item in items)), 'trials': len(items)})
            crypto_values.append(crypto)
            hash_exact = hash_exact and all((bool(item['hash_calls_exact']) for item in items))
        per_distribution.append({'distribution': distribution, 'shard_points': len(by_shards), 'crypto_relative_range': relative_range(crypto_values), 'hash_calls_exact': hash_exact})
    evaluated = bool(per_distribution) and all((item['shard_points'] >= 3 for item in per_distribution))
    passed = evaluated and all((item['crypto_relative_range'] <= 0.3 and item['hash_calls_exact'] for item in per_distribution))
    check = {'hypothesis': 'H4c', 'description': 'Shard count changes scheduling/locality but not fixed-epoch Merkle depth or cryptographic work.', 'status': 'PASS' if passed else 'FAIL' if evaluated else 'NOT_EVALUATED', 'thresholds': {'crypto_relative_range': 0.3}, 'per_distribution': per_distribution}
    return (points, check)

def _fairness_analysis(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subset = [row for row in rows if 'workload' in row['sweep_tags'].split(';') and float(row['target_fraction_capacity']) <= 0.95 + 1e-09]
    output: list[dict[str, Any]] = []
    for (distribution,), items in sorted(_group(subset, ('distribution',)).items()):
        output.append({'distribution': distribution, 'observations': len(items), 'minimum_jain_service_ratio': min((float(item['jain_service_ratio']) for item in items)), 'median_jain_service_ratio': _median((item['jain_service_ratio'] for item in items)), 'total_starved_shards': sum((int(item['starved_shards']) for item in items)), 'median_completion_ratio': _median((item['completion_ratio'] for item in items))})
    evaluated = bool(output)
    passed = evaluated and all((item['minimum_jain_service_ratio'] >= 0.95 and item['total_starved_shards'] == 0 for item in output))
    check = {'hypothesis': 'Fairness safeguard', 'description': 'Below the preregistered saturation boundary, no offered shard is starved and completion-ratio fairness remains high.', 'status': 'PASS' if passed else 'FAIL' if evaluated else 'NOT_EVALUATED', 'thresholds': {'minimum_jain_service_ratio': 0.95, 'starved_shards': 0}}
    return (output, check)

def _gaussian_solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            augmented[pivot][column] += 1e-09
        augmented[column], augmented[pivot] = (augmented[pivot], augmented[column])
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [value - factor * pivot_value for value, pivot_value in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(n)]

def _factorial_model(rows: Sequence[dict[str, Any]], response: str) -> dict[str, Any]:
    subset = [row for row in rows if 'factorial' in row['sweep_tags'].split(';') and row['mode'] == 'full']
    names = ('intercept', 'log2_shards', 'log10_history', 'log2_clients', 'shards_x_history', 'shards_x_clients', 'history_x_clients')
    design: list[list[float]] = []
    targets: list[float] = []
    for row in subset:
        y = float(row[response])
        if y <= 0 or not math.isfinite(y):
            continue
        shards = math.log2(float(row['shard_count']))
        history = math.log10(float(row['historical_epochs']))
        clients = math.log2(float(row['concurrent_clients']))
        design.append([1.0, shards, history, clients, shards * history, shards * clients, history * clients])
        targets.append(math.log(y))
    if len(design) <= len(names):
        return {'response': response, 'status': 'NOT_EVALUATED', 'observations': len(design)}
    size = len(names)
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for row, target in zip(design, targets):
        for i in range(size):
            xty[i] += row[i] * target
            for j in range(size):
                xtx[i][j] += row[i] * row[j]
    for index in range(size):
        xtx[index][index] += 1e-10
    coefficients = _gaussian_solve(xtx, xty)
    predictions = [sum((coef * value for coef, value in zip(coefficients, row))) for row in design]
    mean_target = statistics.fmean(targets)
    sse = sum(((actual - predicted) ** 2 for actual, predicted in zip(targets, predictions)))
    sst = sum(((actual - mean_target) ** 2 for actual in targets))
    return {'response': response, 'status': 'PASS', 'observations': len(targets), 'response_transform': 'natural_log', 'r2': 1.0 - sse / sst if sst else 1.0, 'rmse_log': math.sqrt(sse / len(targets)), 'coefficients': dict(zip(names, coefficients))}

def _write_figures(output_dir: Path, configuration_rows: Sequence[dict[str, Any]], history_rows: Sequence[dict[str, Any]], core_rows: Sequence[dict[str, Any]], fairness_rows: Sequence[dict[str, Any]]) -> None:
    concurrency = [row for row in configuration_rows if 'concurrency_ramp' in row['sweep_tags'].split(';')]
    throughput_series: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for (distribution, clients), items in _group(concurrency, ('distribution', 'concurrent_clients')).items():
        maximum = max((float(item['throughput_qps_median']) for item in items))
        throughput_series[str(distribution)].append((float(clients), maximum))
    line_chart(output_dir / 'fig_E4_throughput_vs_concurrency.svg', title='Accepted Throughput versus Concurrent Clients', x_label='Concurrent clients', y_label='Maximum accepted throughput (queries/s)', series=throughput_series, x_log2=True)
    tail_series = {'maximum throughput': [(float(row['verifier_workers']), float(row['maximum_throughput_qps_median'])) for row in core_rows]}
    line_chart(output_dir / 'fig_E4_core_scaling.svg', title='Verifier-Core Scaling', x_label='Verifier worker processes / allocated cores', y_label='Maximum accepted throughput (queries/s)', series=tail_series, x_log2=True)
    history_series: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for row in history_rows:
        history_series[f"lookup ({row['distribution']})"].append((float(row['historical_epochs']), float(row['lookup_p50_us_median'])))
        history_series[f"crypto ({row['distribution']})"].append((float(row['historical_epochs']), float(row['crypto_pipeline_p50_us_median'])))
    line_chart(output_dir / 'fig_E4_latency_vs_history.svg', title='Indexed Lookup and Verification versus Historical Depth', x_label='Historical epochs per shard', y_label='Median local phase time (microseconds)', series=history_series, x_log2=True)
    fairness_series = {'Jain service-ratio index': [(float(index + 1), float(row['minimum_jain_service_ratio'])) for index, row in enumerate(fairness_rows)]}
    labels = [(float(index + 1), row['distribution']) for index, row in enumerate(fairness_rows)]
    line_chart(output_dir / 'fig_E4_workload_fairness.svg', title='Per-Shard Service Fairness below Saturation', x_label='Workload distribution', y_label='Minimum Jain fairness index', series=fairness_series, x_tick_labels=labels)

def _manifest(output_dir: Path) -> None:
    lines: list[str] = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != 'manifest.sha256':
            lines.append(f'{sha256_file(path)}  {path.name}')
    atomic_write_text(output_dir / 'manifest.sha256', '\n'.join(lines) + '\n')

def analyze(plan_path: str | Path, raw_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = inspect_raw(plan_path, raw_dir)
    results = audit.pop('results')
    rows = _step_rows(results)
    configurations = _configuration_summary(rows)
    saturation = _saturation_rows(rows)
    history_rows, h4a = _history_analysis(rows)
    core_rows, h4b = _core_analysis(rows, saturation)
    shard_rows, h4c = _shard_analysis(rows)
    fairness_rows, fairness_check = _fairness_analysis(rows)
    all_case_integrity = all((result.get('checks', {}).get('all_queries_accepted') and result.get('checks', {}).get('hash_calls_exact') and all((code == 0 for code in result.get('checks', {}).get('worker_exit_codes', []))) for result in results))
    below_saturation = [row for row in rows if float(row['target_fraction_capacity']) <= 0.95 + 1e-09]
    service_headroom_fraction = _mean((float(row['service_headroom_pass']) for row in below_saturation))
    service_check = {'hypothesis': 'Retrieval-service headroom', 'description': 'The retrieval tier remains outside the bottleneck regime for the primary below-saturation observations.', 'status': 'PASS' if below_saturation and service_headroom_fraction >= 0.95 else 'FAIL' if below_saturation else 'NOT_EVALUATED', 'metrics': {'headroom_fraction': service_headroom_fraction, 'required_fraction': 0.95}}
    hypotheses = [h4a, h4b, h4c, fairness_check, service_check]
    primary = [h4a, h4b, h4c]
    all_primary_pass = all((item['status'] == 'PASS' for item in primary))
    full_protocol = audit['complete'] and all_case_integrity
    factorial_models = [_factorial_model(rows, 'throughput_qps'), _factorial_model(rows, 'latency_p95_ms')]
    csv_write(output_dir / 'step_measurements.csv', rows)
    csv_write(output_dir / 'configuration_summary.csv', configurations)
    csv_write(output_dir / 'saturation_summary.csv', saturation)
    csv_write(output_dir / 'history_scaling.csv', history_rows)
    csv_write(output_dir / 'core_scaling.csv', core_rows)
    csv_write(output_dir / 'shard_scaling.csv', shard_rows)
    csv_write(output_dir / 'fairness_summary.csv', fairness_rows)
    csv_write(output_dir / 'hypothesis_checks.csv', [{'hypothesis': item['hypothesis'], 'status': item['status'], 'description': item['description'], 'details_json': json.dumps(_json_safe({key: value for key, value in item.items() if key not in {'hypothesis', 'status', 'description'}}), sort_keys=True)} for item in hypotheses])
    atomic_write_json(output_dir / 'factorial_models.json', _json_safe(factorial_models))
    summary: dict[str, Any] = {'schema_version': 1, 'experiment': 'E4', 'status': 'PASS' if full_protocol else 'FAIL', 'plan_audit': audit, 'valid_cases': len(results), 'rate_step_observations': len(rows), 'measured_accepted_queries': sum((int(row['accepted_total']) for row in rows)), 'all_case_integrity_checks_pass': all_case_integrity, 'all_primary_hypotheses_pass': all_primary_pass, 'hypotheses': hypotheses, 'factorial_models': factorial_models, 'protocol_notes': {'network_profile': 'P1-equivalent target: 10 ms RTT and 100 Mbit/s in publication configuration', 'primary_mode': 'MSI-Full', 'fixed_epoch_length': 512, 'fixed_block_bytes': 2048, 'saturation_definition': 'lowest offered rate whose accepted throughput reaches at least 95% of the observed maximum', 'raw_query_files': 'gzip-compressed per-query records under raw/queries'}}
    atomic_write_json(output_dir / 'summary.json', _json_safe(summary))
    report_lines = ['# Experiment E4 — Concurrent Multi-Shard and Long-History Scaling', '', f"- Raw-plan completeness: **{('PASS' if audit['complete'] else 'FAIL')}** ({audit['valid_cases']}/{audit['expected_cases']} cases).", f"- Case-level cryptographic and acceptance checks: **{('PASS' if all_case_integrity else 'FAIL')}**.", f"- Accepted measured queries represented in the analysis: **{summary['measured_accepted_queries']:,}**.", f"- H4a historical-depth independence: **{h4a['status']}**.", f"- H4b verifier-core scaling and saturation: **{h4b['status']}**.", f"- H4c fixed-depth multi-shard scaling: **{h4c['status']}**.", f"- Fairness safeguard: **{fairness_check['status']}**.", f"- Retrieval-service headroom: **{service_check['status']}**.", '', 'The analysis does not alter preregistered thresholds when a hypothesis fails. A complete run may therefore have `status=PASS` for data integrity while `all_primary_hypotheses_pass=false` for the scientific conclusion.', '', '## Principal output files', '', '`step_measurements.csv`, `configuration_summary.csv`, `saturation_summary.csv`, `history_scaling.csv`, `core_scaling.csv`, `shard_scaling.csv`, `fairness_summary.csv`, `hypothesis_checks.csv`, and the four SVG figures contain the values required for the thesis evaluation chapter.']
    atomic_write_text(output_dir / 'report.md', '\n'.join(report_lines) + '\n')
    _write_figures(output_dir, configurations, history_rows, core_rows, fairness_rows)
    _manifest(output_dir)
    return _json_safe(summary)

def validate_results(results_dir: str | Path) -> dict[str, Any]:
    results_dir = Path(results_dir)
    required = {'summary.json', 'report.md', 'step_measurements.csv', 'configuration_summary.csv', 'saturation_summary.csv', 'history_scaling.csv', 'core_scaling.csv', 'shard_scaling.csv', 'fairness_summary.csv', 'hypothesis_checks.csv', 'factorial_models.json', 'fig_E4_throughput_vs_concurrency.svg', 'fig_E4_core_scaling.svg', 'fig_E4_latency_vs_history.svg', 'fig_E4_workload_fairness.svg', 'manifest.sha256'}
    missing = sorted((name for name in required if not (results_dir / name).is_file()))
    mismatches: list[str] = []
    manifest = results_dir / 'manifest.sha256'
    if manifest.is_file():
        for line in manifest.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            digest, name = line.split(None, 1)
            name = name.strip().lstrip('*')
            path = results_dir / name
            if not path.is_file() or sha256_file(path) != digest:
                mismatches.append(name)
    summary = read_json(results_dir / 'summary.json') if (results_dir / 'summary.json').is_file() else {}
    complete = bool(summary.get('plan_audit', {}).get('complete'))
    integrity = bool(summary.get('all_case_integrity_checks_pass'))
    status = 'PASS' if not missing and (not mismatches) and complete and integrity else 'FAIL'
    return {'status': status, 'missing_files': missing, 'manifest_mismatches': mismatches, 'plan_complete': complete, 'case_integrity': integrity, 'all_primary_hypotheses_pass': summary.get('all_primary_hypotheses_pass')}
