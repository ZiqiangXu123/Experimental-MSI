from __future__ import annotations
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from .benchmark import case_variant, result_filename
from .config import read_plan
from .svg import line_chart
from .util import atomic_write_json, atomic_write_text, bootstrap_ci, next_power_of_two, percentile, read_json, sha256_file, summarise

def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def _linreg(xs: list[float], ys: list[float]) -> dict[str, float | int]:
    if len(xs) != len(ys) or len(xs) < 2:
        return {'n': len(xs), 'slope': math.nan, 'intercept': math.nan, 'r2': math.nan, 'rmse': math.nan}
    xbar = statistics.fmean(xs)
    ybar = statistics.fmean(ys)
    sxx = sum(((x - xbar) ** 2 for x in xs))
    if sxx == 0:
        return {'n': len(xs), 'slope': math.nan, 'intercept': ybar, 'r2': math.nan, 'rmse': math.nan}
    slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, ys))) / sxx
    intercept = ybar - slope * xbar
    preds = [intercept + slope * x for x in xs]
    residuals = [y - p for y, p in zip(ys, preds)]
    ss_res = sum((r * r for r in residuals))
    ss_tot = sum(((y - ybar) ** 2 for y in ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    rmse = math.sqrt(ss_res / len(xs))
    return {'n': len(xs), 'slope': slope, 'intercept': intercept, 'r2': r2, 'rmse': rmse}

def _server_path(raw_dir: Path, declared: str) -> Path:
    path = Path(declared)
    if path.exists():
        return path
    fallback = raw_dir / 'services' / path.name
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f'server metrics not found: {declared}')

def _expected_ids(plan_blocks: list[dict[str, Any]]) -> set[str]:
    return {str(case['case_id']) for block in plan_blocks for case in block['cases']}

def _load_results(raw_dir: Path, plan_blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected = _expected_ids(plan_blocks)
    results: list[dict[str, Any]] = []
    corrupt: list[str] = []
    actual: set[str] = set()
    for path in sorted(raw_dir.glob('*.json.gz')):
        try:
            obj = read_json(path)
            case_id = str(obj['case']['case_id'])
            if obj.get('experiment') != 'E3':
                raise ValueError('wrong experiment')
            if not isinstance(obj.get('checks'), dict) or not all(obj['checks'].values()):
                raise ValueError('result internal checks failed')
            results.append(obj)
            actual.add(case_id)
        except Exception as exc:
            corrupt.append(f'{path.name}: {type(exc).__name__}: {exc}')
    return (results, {'expected_cases': len(expected), 'actual_cases': len(actual), 'missing_case_ids': sorted(expected - actual), 'extra_case_ids': sorted(actual - expected), 'corrupt_results': corrupt, 'complete': actual == expected and (not corrupt)})

def _wire_rows(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in results:
        if obj.get('kind') != 'wire_audit':
            continue
        case = obj['case']
        rows.append({'case_id': case['case_id'], 'variant': obj['variant'], 'scheme': case['scheme'], 'deployment': case['deployment'], 'epoch_length_n': case['epoch_length'], 'padded_length_n_prime': obj['expected']['n_prime'], 'depth': obj['expected']['depth'], 'block_bytes': case['block_bytes'], **obj['wire'], 'expected_witness_bytes': obj['expected']['witness_bytes']})
    return rows

def _calibration_rows(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in results:
        if obj.get('kind') != 'network_trial':
            continue
        case = obj['case']
        configured_ns = int(round(float(obj['profile']['rtt_ms']) * 1000000.0))
        for role, calibration in sorted(obj.get('calibration', {}).items()):
            samples = [float(value) for value in calibration.get('samples_ns', [])]
            median_ns = int(calibration.get('median_ns', 0))
            rows.append({'case_id': case['case_id'], 'sweep': case['sweep'], 'trial': case['trial'], 'variant': obj['variant'], 'profile': case['profile'], 'role': role, 'configured_rtt_ms': configured_ns / 1000000.0, 'physical_rtt_median_ms': median_ns / 1000000.0, 'physical_rtt_p95_ms': percentile(samples, 95) / 1000000.0 if samples else math.nan, 'injected_rtt_ms': max(0, configured_ns - median_ns) / 1000000.0, 'configured_target_feasible': int(median_ns <= configured_ns), 'calibration_pings': len(samples)})
    return rows

def _query_rows(raw_dir: Path, results: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    service_audit: list[dict[str, Any]] = []
    for obj in results:
        if obj.get('kind') != 'network_trial':
            continue
        case = obj['case']
        service_maps: dict[str, dict[int, dict[str, Any]]] = {}
        for role, declared in obj['server_metric_files'].items():
            service = read_json(_server_path(raw_dir, declared))
            checks_ok = isinstance(service.get('checks'), dict) and all(service['checks'].values())
            service_audit.append({'case_id': case['case_id'], 'role': role, 'expected_queries': service.get('expected_queries'), 'completed_queries': service.get('completed_queries'), 'checks_pass': checks_ok, 'error': service.get('error')})
            if not checks_ok:
                raise RuntimeError(f"server checks failed for {case['case_id']} role={role}")
            service_maps[role] = {int(item['request_id']): item for item in service['query_metrics']}
        profile = obj['profile']
        bandwidth_bps = float(profile['bandwidth_mbps']) * 1000000.0
        deadlines = [float(x) for x in case.get('deadlines_ms', [])]
        for query in obj['timing']['raw_queries']:
            request_id = int(query['request_id'])
            software_ns = int(query['response_deserialize_ns']) + int(query['local_verify_ns'])
            baseline_total_ns = 0
            request_target_ns = 0
            response_target_ns = 0
            request_actual_delay_ns = 0
            response_actual_delay_ns = 0
            base_injected_serial_ns = 0
            target_feasible = True
            response_breakdown = defaultdict(int)
            response_loss_rounds = 0
            retransmitted_packets = 0
            server_roles: list[str] = []
            for txn in query['transactions']:
                role = str(txn['role'])
                server_roles.append(role)
                server = service_maps[role].get(request_id)
                if server is None:
                    raise RuntimeError(f"missing server metric case={case['case_id']} role={role} request={request_id}")
                software_ns += int(txn['request_serialize_ns'])
                software_ns += int(txn['request_socket_send_ns'])
                software_ns += int(server['request_deserialize_ns'])
                software_ns += int(server['service_lookup_ns'])
                software_ns += int(server['response_serialize_ns'])
                software_ns += int(server['response_socket_send_ns'])
                baseline = int(obj['calibration'][role]['median_ns'])
                baseline_total_ns += baseline
                configured_ns = int(round(float(profile['rtt_ms']) * 1000000.0))
                target_feasible = target_feasible and baseline <= configured_ns
                req_link = txn['request_link']
                resp_link = server['response_link']
                request_target_ns += int(req_link['total_delay_ns'])
                response_target_ns += int(resp_link['total_delay_ns'])
                request_actual_delay_ns += int(req_link.get('actual_delay_ns') or 0)
                response_actual_delay_ns += int(resp_link.get('actual_delay_ns') or 0)
                base_injected_serial_ns += int(req_link['propagation_delay_ns'])
                base_injected_serial_ns += int(round(int(req_link['application_bytes']) * 8.0 / bandwidth_bps * 1000000000.0))
                base_injected_serial_ns += int(resp_link['propagation_delay_ns'])
                base_injected_serial_ns += int(round(int(resp_link['application_bytes']) * 8.0 / bandwidth_bps * 1000000000.0))
                response_loss_rounds += int(resp_link['retransmission_rounds'])
                retransmitted_packets += int(req_link['retransmitted_packets']) + int(resp_link['retransmitted_packets'])
                for key, value in server['wire_breakdown'].items():
                    response_breakdown[key] += int(value)
            predicted_base_ns = software_ns + baseline_total_ns + base_injected_serial_ns
            predicted_loss_ns = software_ns + baseline_total_ns + request_target_ns + response_target_ns
            actual_ns = int(query['end_to_end_ns'])
            emulation_target_ns = request_target_ns + response_target_ns
            emulation_actual_ns = request_actual_delay_ns + response_actual_delay_ns
            payload_bytes = response_breakdown['payload_bytes']
            witness_bytes = response_breakdown['witness_bytes']
            anchor_bytes = response_breakdown['anchor_evidence_bytes']
            verified_useful_bytes = payload_bytes + witness_bytes + anchor_bytes
            row: dict[str, Any] = {'case_id': case['case_id'], 'sweep': case['sweep'], 'trial': case['trial'], 'variant': obj['variant'], 'scheme': case['scheme'], 'deployment': case['deployment'], 'profile': case['profile'], 'profile_name': profile['name'], 'configured_rtt_ms': profile['rtt_ms'], 'bandwidth_mbps': profile['bandwidth_mbps'], 'loss_rate': profile['loss_rate'], 'epoch_length_n': case['epoch_length'], 'padded_length_n_prime': next_power_of_two(int(case['epoch_length'])), 'block_bytes': case['block_bytes'], 'request_id': request_id, 'position': query['position'], 'transaction_count': query['transaction_count'], 'server_roles': '+'.join(server_roles), 'request_bytes': query['total_request_bytes'], 'total_response_bytes': query['total_response_bytes'], 'payload_bytes': payload_bytes, 'witness_bytes': witness_bytes, 'anchor_evidence_bytes': anchor_bytes, 'metadata_bytes': response_breakdown['metadata_bytes'], 'framing_bytes': response_breakdown['framing_bytes'], 'actual_end_to_end_ns': actual_ns, 'local_verify_ns': query['local_verify_ns'], 'software_model_ns': software_ns, 'physical_baseline_rtt_total_ns': baseline_total_ns, 'physical_baseline_rtt_per_transaction_ns': baseline_total_ns / max(1, int(query['transaction_count'])), 'configured_rtt_target_feasible': int(target_feasible), 'emulation_target_ns': emulation_target_ns, 'emulation_actual_ns': emulation_actual_ns, 'emulation_overshoot_ns': emulation_actual_ns - emulation_target_ns, 'predicted_base_ns': predicted_base_ns, 'predicted_loss_aware_ns': predicted_loss_ns, 'base_residual_ns': actual_ns - predicted_base_ns, 'loss_aware_residual_ns': actual_ns - predicted_loss_ns, 'retransmission_rounds': response_loss_rounds, 'retransmitted_packets': retransmitted_packets, 'payload_goodput_mbps': payload_bytes * 8.0 / actual_ns * 1000.0, 'verified_goodput_mbps': verified_useful_bytes * 8.0 / actual_ns * 1000.0, 'accepted': query['accepted']}
            for deadline in deadlines:
                row[f'complete_by_{deadline:g}ms'] = int(actual_ns <= deadline * 1000000.0)
            rows.append(row)
    return (rows, service_audit)

def _config_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row['sweep'], row['variant'], row['profile'], row['epoch_length_n'], row['block_bytes'])

def _launch_rows(query_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in query_rows:
        groups[str(row['case_id'])].append(row)
    out: list[dict[str, Any]] = []
    for case_id, items in sorted(groups.items()):
        first = items[0]
        actual = [float(x['actual_end_to_end_ns']) for x in items]
        predicted = [float(x['predicted_loss_aware_ns']) for x in items]
        out.append({'case_id': case_id, 'sweep': first['sweep'], 'trial': first['trial'], 'variant': first['variant'], 'profile': first['profile'], 'epoch_length_n': first['epoch_length_n'], 'block_bytes': first['block_bytes'], 'queries': len(items), 'median_ms': statistics.median(actual) / 1000000.0, 'p95_ms': percentile(actual, 95) / 1000000.0, 'p99_ms': percentile(actual, 99) / 1000000.0, 'predicted_median_ms': statistics.median(predicted) / 1000000.0, 'median_loss_aware_residual_ms': statistics.median((float(x['loss_aware_residual_ns']) for x in items)) / 1000000.0, 'median_payload_goodput_mbps': statistics.median((float(x['payload_goodput_mbps']) for x in items))})
    return out

def _aggregate_rows(query_rows: list[dict[str, Any]], launch_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    launches: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in query_rows:
        groups[_config_key(row)].append(row)
    for row in launch_rows:
        launches[row['sweep'], row['variant'], row['profile'], row['epoch_length_n'], row['block_bytes']].append(row)
    out: list[dict[str, Any]] = []
    for key, items in sorted(groups.items(), key=lambda kv: tuple((str(x) for x in kv[0]))):
        first = items[0]
        actual = [float(x['actual_end_to_end_ns']) for x in items]
        predicted_base = [float(x['predicted_base_ns']) for x in items]
        predicted_loss = [float(x['predicted_loss_aware_ns']) for x in items]
        launch_medians = [float(x['median_ms']) for x in launches[key]]
        ci_lo, ci_hi = bootstrap_ci(launch_medians, statistic='median', resamples=2000, seed=202603)
        row = {'sweep': first['sweep'], 'variant': first['variant'], 'scheme': first['scheme'], 'deployment': first['deployment'], 'profile': first['profile'], 'profile_name': first['profile_name'], 'configured_rtt_ms': first['configured_rtt_ms'], 'bandwidth_mbps': first['bandwidth_mbps'], 'loss_rate': first['loss_rate'], 'epoch_length_n': first['epoch_length_n'], 'padded_length_n_prime': first['padded_length_n_prime'], 'block_bytes': first['block_bytes'], 'independent_launches': len(launches[key]), 'queries': len(items), 'median_ms': statistics.median(actual) / 1000000.0, 'p95_ms': percentile(actual, 95) / 1000000.0, 'p99_ms': percentile(actual, 99) / 1000000.0, 'launch_median_95ci_low_ms': ci_lo, 'launch_median_95ci_high_ms': ci_hi, 'predicted_base_median_ms': statistics.median(predicted_base) / 1000000.0, 'predicted_loss_aware_median_ms': statistics.median(predicted_loss) / 1000000.0, 'base_residual_median_ms': statistics.median((float(x['base_residual_ns']) for x in items)) / 1000000.0, 'loss_aware_residual_median_ms': statistics.median((float(x['loss_aware_residual_ns']) for x in items)) / 1000000.0, 'loss_aware_residual_p95_abs_ms': percentile([abs(float(x['loss_aware_residual_ns'])) for x in items], 95) / 1000000.0, 'request_bytes': first['request_bytes'], 'payload_bytes': first['payload_bytes'], 'witness_bytes': first['witness_bytes'], 'anchor_evidence_bytes': first['anchor_evidence_bytes'], 'metadata_bytes': first['metadata_bytes'], 'framing_bytes': first['framing_bytes'], 'total_response_bytes': first['total_response_bytes'], 'median_payload_goodput_mbps': statistics.median((float(x['payload_goodput_mbps']) for x in items)), 'median_verified_goodput_mbps': statistics.median((float(x['verified_goodput_mbps']) for x in items)), 'queries_with_retransmission_pct': 100.0 * sum((int(x['retransmitted_packets']) > 0 for x in items)) / len(items), 'physical_baseline_rtt_median_ms': statistics.median((float(x['physical_baseline_rtt_per_transaction_ns']) for x in items)) / 1000000.0, 'configured_rtt_target_feasible_pct': 100.0 * sum((int(x['configured_rtt_target_feasible']) for x in items)) / len(items), 'emulation_target_median_ms': statistics.median((float(x['emulation_target_ns']) for x in items)) / 1000000.0, 'emulation_actual_median_ms': statistics.median((float(x['emulation_actual_ns']) for x in items)) / 1000000.0, 'emulation_overshoot_median_ms': statistics.median((float(x['emulation_overshoot_ns']) for x in items)) / 1000000.0, 'emulation_overshoot_p95_abs_ms': percentile([abs(float(x['emulation_overshoot_ns'])) for x in items], 95) / 1000000.0}
        deadline_keys = sorted((k for k in first if k.startswith('complete_by_')))
        for deadline_key in deadline_keys:
            row[deadline_key + '_pct'] = 100.0 * sum((int(x[deadline_key]) for x in items)) / len(items)
        out.append(row)
    return out

def _hypotheses(wire_rows: list[dict[str, Any]], aggregate: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    fits: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    h3a_exact = bool(wire_rows) and all((int(row['witness_bytes']) == int(row['expected_witness_bytes']) for row in wire_rows))
    fit_results: dict[str, dict[str, Any]] = {}
    for variant in sorted({str(r['variant']) for r in wire_rows}):
        rows = [r for r in wire_rows if r['variant'] == variant]
        if variant == 'B3_leaf':
            x_name = 'n_prime'
            xs = [float(r['padded_length_n_prime']) for r in rows]
        else:
            x_name = 'log2_n_prime'
            xs = [math.log2(float(r['padded_length_n_prime'])) for r in rows]
        ys = [float(r['witness_bytes']) for r in rows]
        fit = _linreg(xs, ys)
        fit_results[variant] = fit
        fits.append({'hypothesis': 'H3a', 'series': variant, 'x': x_name, **fit})
    h3a_fit = bool(fit_results) and all((float(f['r2']) >= 0.999999 for f in fit_results.values()))
    checks.append({'hypothesis': 'H3a', 'criterion': 'Exact wire witness formula and scaling fits', 'status': 'PASS' if h3a_exact and h3a_fit else 'FAIL', 'observed': f'exact={h3a_exact}; all_R2>=0.999999={h3a_fit}', 'threshold': "path=5+33log2(n'); leaf=5+32n'; R2>=0.999999"})
    xs = [float(r['predicted_loss_aware_median_ms']) for r in aggregate]
    ys = [float(r['median_ms']) for r in aggregate]
    model_fit = _linreg(xs, ys)
    if xs:
        nmae = statistics.fmean((abs(y - x) for x, y in zip(xs, ys))) / max(statistics.fmean(ys), 1e-12)
    else:
        nmae = math.nan
    fits.append({'hypothesis': 'H3b', 'series': 'measured_vs_loss_aware_prediction', 'x': 'predicted_ms', **model_fit, 'nmae': nmae})
    h3b_evaluable = len(xs) >= 8
    h3b_pass = h3b_evaluable and float(model_fit['r2']) >= 0.98 and (0.9 <= float(model_fit['slope']) <= 1.1) and (nmae <= 0.1)
    checks.append({'hypothesis': 'H3b', 'criterion': 'Measured median latency follows calibrated loss-aware link model', 'status': 'PASS' if h3b_pass else 'NOT_EVALUATED' if not h3b_evaluable else 'FAIL', 'observed': f"R2={model_fit['r2']:.6g}; slope={model_fit['slope']:.6g}; NMAE={nmae:.6g}", 'threshold': 'N>=8, R2>=0.98, slope in [0.90,1.10], NMAE<=0.10'})
    cells: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in aggregate:
        cells[row['sweep'], row['epoch_length_n'], row['block_bytes'], row['profile']].append(row)
    dispersion_by_profile: dict[str, list[float]] = defaultdict(list)
    leaf_full_gap_by_profile: dict[str, list[float]] = defaultdict(list)
    for (_sweep, _n, _b, profile), rows in cells.items():
        by_variant = {r['variant']: r for r in rows}
        if len(by_variant) >= 3:
            values = [float(r['p95_ms']) for r in rows]
            dispersion_by_profile[str(profile)].append(max(values) - min(values))
        if 'B3_leaf' in by_variant and 'B2_full' in by_variant:
            leaf_full_gap_by_profile[str(profile)].append(float(by_variant['B3_leaf']['p95_ms']) - float(by_variant['B2_full']['p95_ms']))
    dispersion_median = {p: statistics.median(v) for p, v in dispersion_by_profile.items() if v}
    p0 = dispersion_median.get('P0')
    p3 = dispersion_median.get('P3')
    h3c_evaluable = p0 is not None and p3 is not None
    ratio = p3 / max(p0, 1e-09) if h3c_evaluable else math.nan
    h3c_pass = bool(h3c_evaluable and p3 > p0 and (ratio >= 2.0) and (p3 == max(dispersion_median.values())))
    checks.append({'hypothesis': 'H3c', 'criterion': 'Mode p95 dispersion is strongest under P3 low-bandwidth adverse link', 'status': 'PASS' if h3c_pass else 'NOT_EVALUATED' if not h3c_evaluable else 'FAIL', 'observed': f'P0={p0}; P3={p3}; ratio={ratio}', 'threshold': 'P3 is maximum and P3/P0>=2'})
    diagnostics = {'wire_exact': h3a_exact, 'wire_fits': fit_results, 'latency_model_fit': model_fit, 'latency_model_nmae': nmae, 'dispersion_median_ms_by_profile': dispersion_median, 'leaf_full_gap_median_ms_by_profile': {p: statistics.median(v) for p, v in leaf_full_gap_by_profile.items() if v}}
    return (checks, fits, diagnostics)

def _split_penalty(aggregate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in aggregate:
        key = (row['sweep'], row['profile'], row['epoch_length_n'], row['block_bytes'])
        groups[key][str(row['variant'])] = row
    out: list[dict[str, Any]] = []
    for key, variants in sorted(groups.items(), key=lambda kv: tuple((str(x) for x in kv[0]))):
        if 'B4_ext_coalesced' not in variants or 'B4_ext_split' not in variants:
            continue
        co = variants['B4_ext_coalesced']
        sp = variants['B4_ext_split']
        out.append({'sweep': key[0], 'profile': key[1], 'epoch_length_n': key[2], 'block_bytes': key[3], 'configured_rtt_ms': co['configured_rtt_ms'], 'coalesced_median_ms': co['median_ms'], 'split_median_ms': sp['median_ms'], 'median_penalty_ms': float(sp['median_ms']) - float(co['median_ms']), 'coalesced_p95_ms': co['p95_ms'], 'split_p95_ms': sp['p95_ms'], 'p95_penalty_ms': float(sp['p95_ms']) - float(co['p95_ms']), 'extra_request_bytes': int(sp['request_bytes']) - int(co['request_bytes']), 'extra_response_bytes': int(sp['total_response_bytes']) - int(co['total_response_bytes'])})
    return out

def _heatmap(path: Path, aggregate: list[dict[str, Any]]) -> None:
    data = [r for r in aggregate if r['sweep'] == 'n_scaling' and int(r['block_bytes']) == 2048]
    variants = [v for v in ('B2_full', 'B3_leaf', 'B4_ext_coalesced', 'B4_ext_split') if any((r['variant'] == v for r in data))]
    ns = sorted({int(r['epoch_length_n']) for r in data})
    profiles = [p for p in ('P0', 'P1', 'P2', 'P3') if any((r['profile'] == p for r in data))]
    if not data or not variants or (not ns) or (not profiles):
        atomic_write_text(path, "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='180'><text x='20' y='40'>No heat-map data</text></svg>\n")
        return
    width = 1050
    panel_h = 170
    height = 80 + panel_h * len(variants)
    left = 150
    cell_w = (width - left - 40) / len(ns)
    cell_h = 26
    values = [float(r['p95_ms']) for r in data]
    vmax = max(values)
    vmin = min(values)

    def shade(value: float) -> str:
        frac = 0.0 if vmax == vmin else (math.log1p(value) - math.log1p(vmin)) / (math.log1p(vmax) - math.log1p(vmin))
        c = int(round(245 - 180 * frac))
        return f'rgb(255,{c},{c})'
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", "<rect width='100%' height='100%' fill='white'/>", f"<text x='{width / 2}' y='32' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='bold'>E3 p95 end-to-end latency heat map (ms)</text>"]
    lookup = {(r['variant'], r['profile'], int(r['epoch_length_n'])): float(r['p95_ms']) for r in data}
    for vi, variant in enumerate(variants):
        top = 60 + vi * panel_h
        out.append(f"<text x='15' y='{top + 18}' font-family='sans-serif' font-size='16' font-weight='bold'>{variant}</text>")
        for pi, profile in enumerate(profiles):
            y = top + 32 + pi * cell_h
            out.append(f"<text x='{left - 10}' y='{y + 18}' text-anchor='end' font-family='sans-serif' font-size='12'>{profile}</text>")
            for ni, n in enumerate(ns):
                x = left + ni * cell_w
                value = lookup.get((variant, profile, n), math.nan)
                fill = '#eeeeee' if math.isnan(value) else shade(value)
                label = 'NA' if math.isnan(value) else f'{value:.2f}'
                out.append(f"<rect x='{x:.2f}' y='{y:.2f}' width='{cell_w - 2:.2f}' height='{cell_h - 2}' fill='{fill}' stroke='white'/>")
                out.append(f"<text x='{x + (cell_w - 2) / 2:.2f}' y='{y + 17}' text-anchor='middle' font-family='sans-serif' font-size='11'>{label}</text>")
        for ni, n in enumerate(ns):
            x = left + ni * cell_w + (cell_w - 2) / 2
            out.append(f"<text x='{x:.2f}' y='{top + 32 + len(profiles) * cell_h + 18}' text-anchor='middle' font-family='sans-serif' font-size='11'>{n}</text>")
    out.append('</svg>')
    atomic_write_text(path, '\n'.join(out) + '\n')

def _scatter(path: Path, aggregate: list[dict[str, Any]]) -> None:
    if not aggregate:
        atomic_write_text(path, "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='180'><text x='20' y='40'>No data</text></svg>\n")
        return
    xs = [float(r['predicted_loss_aware_median_ms']) for r in aggregate]
    ys = [float(r['median_ms']) for r in aggregate]
    lo = min(xs + ys)
    hi = max(xs + ys)
    if hi == lo:
        hi = lo + 1
    width, height = (850, 650)
    left, right, top, bottom = (95, 35, 65, 85)
    pw, ph = (width - left - right, height - top - bottom)

    def xp(v: float) -> float:
        return left + (v - lo) / (hi - lo) * pw

    def yp(v: float) -> float:
        return top + (hi - v) / (hi - lo) * ph
    colours = {'P0': '#1f77b4', 'P1': '#2ca02c', 'P2': '#ff7f0e', 'P3': '#d62728'}
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", "<rect width='100%' height='100%' fill='white'/>", f"<text x='{width / 2}' y='32' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='bold'>Measured versus calibrated link-model latency</text>"]
    for i in range(6):
        v = lo + i * (hi - lo) / 5
        x = xp(v)
        y = yp(v)
        out.append(f"<line x1='{left}' y1='{y:.2f}' x2='{width - right}' y2='{y:.2f}' stroke='#e0e0e0'/>")
        out.append(f"<line x1='{x:.2f}' y1='{top}' x2='{x:.2f}' y2='{height - bottom}' stroke='#e0e0e0'/>")
        out.append(f"<text x='{left - 10}' y='{y + 4:.2f}' text-anchor='end' font-family='sans-serif' font-size='11'>{v:.2f}</text>")
        out.append(f"<text x='{x:.2f}' y='{height - bottom + 22}' text-anchor='middle' font-family='sans-serif' font-size='11'>{v:.2f}</text>")
    out.append(f"<line x1='{xp(lo):.2f}' y1='{yp(lo):.2f}' x2='{xp(hi):.2f}' y2='{yp(hi):.2f}' stroke='black' stroke-width='2' stroke-dasharray='6,5'/>")
    for row in aggregate:
        x = float(row['predicted_loss_aware_median_ms'])
        y = float(row['median_ms'])
        c = colours.get(str(row['profile']), '#666')
        out.append(f"<circle cx='{xp(x):.2f}' cy='{yp(y):.2f}' r='4' fill='{c}' fill-opacity='0.75'/>")
    out.append(f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height - bottom}' stroke='black'/>")
    out.append(f"<line x1='{left}' y1='{height - bottom}' x2='{width - right}' y2='{height - bottom}' stroke='black'/>")
    out.append(f"<text x='{left + pw / 2}' y='{height - 25}' text-anchor='middle' font-family='sans-serif' font-size='15'>Predicted median latency (ms)</text>")
    out.append(f"<text transform='translate(25 {top + ph / 2}) rotate(-90)' text-anchor='middle' font-family='sans-serif' font-size='15'>Measured median latency (ms)</text>")
    for i, p in enumerate(('P0', 'P1', 'P2', 'P3')):
        x = width - right - 150
        y = top + 18 * i
        out.append(f"<circle cx='{x}' cy='{y}' r='4' fill='{colours[p]}'/><text x='{x + 12}' y='{y + 4}' font-family='sans-serif' font-size='12'>{p}</text>")
    out.append('</svg>')
    atomic_write_text(path, '\n'.join(out) + '\n')

def _figures(output: Path, wire_rows: list[dict[str, Any]], aggregate: list[dict[str, Any]], split_rows: list[dict[str, Any]]) -> None:
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in wire_rows:
        series[str(row['variant'])].append((math.log2(float(row['padded_length_n_prime'])), float(row['total_response_bytes'])))
    line_chart(output / 'fig_E3_response_bytes.svg', title='E3 response size by witness representation', x_label="log2(padded epoch length n')", y_label='Total response bytes', series=dict(series), log_y=True)
    _heatmap(output / 'fig_E3_p95_heatmap.svg', aggregate)
    _scatter(output / 'fig_E3_measured_vs_predicted.svg', aggregate)
    split_series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in split_rows:
        if int(row['block_bytes']) == 2048:
            split_series[str(row['profile'])].append((math.log2(float(next_power_of_two(int(row['epoch_length_n'])))), float(row['median_penalty_ms'])))
    line_chart(output / 'fig_E3_split_source_penalty.svg', title='MSI-Ext split-source median latency penalty', x_label="log2(padded epoch length n')", y_label='Split minus coalesced latency (ms)', series=dict(split_series))

def _manifest(output: Path) -> None:
    files = sorted((p for p in output.iterdir() if p.is_file() and p.name != 'manifest.sha256'))
    lines = [f'{sha256_file(path)}  {path.name}' for path in files]
    atomic_write_text(output / 'manifest.sha256', '\n'.join(lines) + '\n')

def analyze(raw_dir: Path, output: Path, plan_path: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    plan = read_plan(plan_path)
    results, plan_audit = _load_results(raw_dir, plan)
    if not plan_audit['complete']:
        raise RuntimeError(f'result set is incomplete: {plan_audit}')
    wire_rows = _wire_rows(results)
    calibration_rows = _calibration_rows(results)
    query_rows, service_audit = _query_rows(raw_dir, results)
    launch_rows = _launch_rows(query_rows)
    aggregate = _aggregate_rows(query_rows, launch_rows)
    checks, fits, diagnostics = _hypotheses(wire_rows, aggregate)
    split_rows = _split_penalty(aggregate)
    _write_csv(output / 'wire_sizes.csv', wire_rows)
    _write_csv(output / 'calibration_summary.csv', calibration_rows)
    _write_csv(output / 'query_measurements.csv', query_rows)
    _write_csv(output / 'launch_summary.csv', launch_rows)
    _write_csv(output / 'latency_summary.csv', aggregate)
    _write_csv(output / 'service_audit.csv', service_audit)
    _write_csv(output / 'model_fits.csv', fits)
    _write_csv(output / 'hypothesis_checks.csv', checks)
    _write_csv(output / 'split_source_penalty.csv', split_rows)
    _figures(output, wire_rows, aggregate, split_rows)
    all_hypotheses = all((row['status'] == 'PASS' for row in checks))
    summary = {'schema_version': 1, 'experiment': 'E3', 'plan_audit': plan_audit, 'wire_audit_cases': len(wire_rows), 'network_query_measurements': len(query_rows), 'network_launches': len(launch_rows), 'network_configurations': len(aggregate), 'service_checks_pass': all((bool(r['checks_pass']) for r in service_audit)), 'calibration_endpoints': len(calibration_rows), 'configured_rtt_target_feasible_pct': 100.0 * sum((int(r['configured_target_feasible']) for r in calibration_rows)) / len(calibration_rows) if calibration_rows else math.nan, 'hypotheses': checks, 'all_primary_hypotheses_pass': all_hypotheses, 'diagnostics': diagnostics, 'interpretation_guard': 'H3 results concern communication sensitivity under 100% eventual service response; outages and withholding belong to E8.'}
    atomic_write_json(output / 'summary.json', summary)
    report = ['# Experiment E3: Communication Cost and Edge-Network Sensitivity', '', f"- Complete planned cases: **{plan_audit['actual_cases']}/{plan_audit['expected_cases']}**", f'- Exact wire-size audits: **{len(wire_rows)}**', f'- Independent network launches: **{len(launch_rows)}**', f'- Measured verified queries: **{len(query_rows)}**', f'- Calibrated service endpoints: **{len(calibration_rows)}**', f"- Nominal RTT targets above the measured physical floor: **{summary['configured_rtt_target_feasible_pct']:.2f}%**", f'- Primary hypotheses all pass: **{all_hypotheses}**', '', '## Pre-registered hypothesis checks', '', '| Hypothesis | Status | Observed | Threshold |', '|---|---:|---|---|']
    for row in checks:
        report.append(f"| {row['hypothesis']} | {row['status']} | {row['observed']} | {row['threshold']} |")
    report.extend(['', '## Scope guard', '', 'Every request in E3 is eventually answered correctly. Virtual loss only adds deterministic retransmission service time and counters; it does not create withholding. Availability failures are therefore not inferred from this experiment.', '', '## Mandatory artifacts', '', '- `wire_sizes.csv` and `fig_E3_response_bytes.svg`', '- `calibration_summary.csv` for the measured physical RTT floor and injected RTT', '- `latency_summary.csv` and `fig_E3_p95_heatmap.svg`', '- `model_fits.csv` and `fig_E3_measured_vs_predicted.svg`', '- `split_source_penalty.csv` and `fig_E3_split_source_penalty.svg`', '- `query_measurements.csv` for all raw per-query observations', ''])
    atomic_write_text(output / 'report.md', '\n'.join(report))
    _manifest(output)
    return summary

def validate_results(output: Path) -> dict[str, Any]:
    required = {'summary.json', 'report.md', 'wire_sizes.csv', 'calibration_summary.csv', 'query_measurements.csv', 'launch_summary.csv', 'latency_summary.csv', 'service_audit.csv', 'model_fits.csv', 'hypothesis_checks.csv', 'split_source_penalty.csv', 'fig_E3_response_bytes.svg', 'fig_E3_p95_heatmap.svg', 'fig_E3_measured_vs_predicted.svg', 'fig_E3_split_source_penalty.svg', 'manifest.sha256'}
    missing = sorted((name for name in required if not (output / name).is_file()))
    if missing:
        raise RuntimeError(f'missing result artifacts: {missing}')
    summary = read_json(output / 'summary.json')
    checks = {'plan_complete': bool(summary['plan_audit']['complete']), 'service_checks_pass': bool(summary['service_checks_pass']), 'wire_audits_present': int(summary['wire_audit_cases']) > 0, 'network_queries_present': int(summary['network_query_measurements']) > 0}
    if not all(checks.values()):
        raise RuntimeError(f'result validation failed: {checks}')
    return {'status': 'PASS', 'checks': checks, 'all_primary_hypotheses_pass': summary['all_primary_hypotheses_pass']}
