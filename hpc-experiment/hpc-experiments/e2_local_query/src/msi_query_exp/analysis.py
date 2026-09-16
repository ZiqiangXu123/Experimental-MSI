from __future__ import annotations
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence
from .config import read_plan
from .svg import line_chart, stacked_bar_chart
from .util import atomic_write_json, atomic_write_text, bootstrap_ci, cliffs_delta, paired_bootstrap_ci, percentile, read_json, sha256_file, summarise
GROUP_FIELDS = ('sweep', 'scheme', 'ablation', 'epoch_length', 'historical_epochs', 'block_bytes', 'layout', 'codec', 'version')
SCHEME_LABELS = {'B0_raw': 'B0 LocalArchive-Raw', 'B1_verified': 'B1 LocalArchive-Verified', 'B2_full': 'B2 MSI-Full', 'B3_leaf': 'B3 MSI-Leaf', 'B4_ext': 'B4 MSI-Ext'}

def _group_key(case: dict[str, Any]) -> tuple[Any, ...]:
    return tuple((case[field] for field in GROUP_FIELDS))

def _group_id(key: tuple[Any, ...]) -> str:
    raw = json.dumps(dict(zip(GROUP_FIELDS, key)), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    fields = list(rows[0])
    extras = sorted({k for row in rows for k in row} - set(fields))
    fields.extend(extras)
    with path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def _simple_fit(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    n = len(xs)
    if n != len(ys) or n < 2:
        return {'n': n, 'status': 'insufficient_data'}
    xbar = statistics.fmean(xs)
    ybar = statistics.fmean(ys)
    sxx = sum(((x - xbar) ** 2 for x in xs))
    if sxx == 0:
        return {'n': n, 'status': 'singular'}
    slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, ys))) / sxx
    intercept = ybar - slope * xbar
    pred = [intercept + slope * x for x in xs]
    residuals = [y - p for y, p in zip(ys, pred)]
    sse = sum((r * r for r in residuals))
    sst = sum(((y - ybar) ** 2 for y in ys))
    r2 = 1.0 - sse / sst if sst > 0 else 1.0
    rmse = math.sqrt(sse / n)
    if n > 2:
        sigma2 = sse / (n - 2)
        slope_se = math.sqrt(sigma2 / sxx)
        intercept_se = math.sqrt(sigma2 * (1 / n + xbar * xbar / sxx))
        slope_ci = (slope - 1.96 * slope_se, slope + 1.96 * slope_se)
        intercept_ci = (intercept - 1.96 * intercept_se, intercept + 1.96 * intercept_se)
    else:
        slope_se = intercept_se = math.nan
        slope_ci = intercept_ci = (math.nan, math.nan)
    return {'n': n, 'status': 'ok', 'intercept': intercept, 'slope': slope, 'intercept_se': intercept_se, 'slope_se': slope_se, 'intercept_ci95_low': intercept_ci[0], 'intercept_ci95_high': intercept_ci[1], 'slope_ci95_low': slope_ci[0], 'slope_ci95_high': slope_ci[1], 'r_squared': r2, 'rmse': rmse, 'max_abs_residual': max((abs(r) for r in residuals))}

def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-18:
            raise ValueError('singular normal matrix')
        augmented[col], augmented[pivot] = (augmented[pivot], augmented[col])
        scale = augmented[col][col]
        augmented[col] = [v / scale for v in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[col])]
    return [augmented[i][-1] for i in range(n)]

def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix)
    columns: list[list[float]] = []
    for i in range(n):
        unit = [0.0] * n
        unit[i] = 1.0
        columns.append(_solve_linear([row[:] for row in matrix], unit))
    return [[columns[col][row] for col in range(n)] for row in range(n)]

def _multiple_fit(rows: Sequence[Sequence[float]], ys: Sequence[float]) -> dict[str, Any]:
    n = len(rows)
    if n != len(ys) or n < 4:
        return {'n': n, 'status': 'insufficient_data'}
    design = [[1.0, *map(float, row)] for row in rows]
    p = len(design[0])
    xtx = [[sum((r[i] * r[j] for r in design)) for j in range(p)] for i in range(p)]
    xty = [sum((r[i] * y for r, y in zip(design, ys))) for i in range(p)]
    try:
        beta = _solve_linear([row[:] for row in xtx], xty)
    except ValueError:
        return {'n': n, 'status': 'singular'}
    pred = [sum((b * x for b, x in zip(beta, row))) for row in design]
    ybar = statistics.fmean(ys)
    residuals = [y - pval for y, pval in zip(ys, pred)]
    sse = sum((r * r for r in residuals))
    sst = sum(((y - ybar) ** 2 for y in ys))
    r2 = 1.0 - sse / sst if sst > 0 else 1.0
    rmse = math.sqrt(sse / n)
    result: dict[str, Any] = {'n': n, 'status': 'ok', 'intercept': beta[0], 'coefficient_1': beta[1], 'coefficient_2': beta[2], 'r_squared': r2, 'rmse': rmse, 'max_abs_residual': max((abs(r) for r in residuals))}
    if n > p:
        sigma2 = sse / (n - p)
        inv = _inverse(xtx)
        for i, name in enumerate(('intercept', 'coefficient_1', 'coefficient_2')):
            se = math.sqrt(max(0.0, sigma2 * inv[i][i]))
            result[f'{name}_se'] = se
            result[f'{name}_ci95_low'] = beta[i] - 1.96 * se
            result[f'{name}_ci95_high'] = beta[i] + 1.96 * se
    return result

def _config_dict(key: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(GROUP_FIELDS, key))

def _aggregate_group(paths: list[Path], key: tuple[Any, ...]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    config = _config_dict(key)
    pooled_acceptance: list[int] = []
    pooled_local: list[int] = []
    phase_values: dict[str, list[int]] = defaultdict(list)
    launch_medians: list[float] = []
    launch_qps: list[float] = []
    launch_cycles: list[float] = []
    cycle_sources: list[str] = []
    valid_energy: list[float] = []
    observed_energy: list[float] = []
    launch_alloc: list[float] = []
    launch_peak_rss: list[float] = []
    first_query: list[float] = []
    launch_rows: list[dict[str, Any]] = []
    operation_hashes: set[int] = set()
    signature_ops: set[int] = set()
    total_queries = 0
    for path in sorted(paths):
        obj = read_json(path)
        case = obj['case']
        acceptance = [int(v) for v in obj['timing']['raw_acceptance_latencies_ns']]
        local = [int(v) for v in obj['timing']['raw_local_query_latencies_ns']]
        pooled_acceptance.extend(acceptance)
        pooled_local.extend(local)
        total_queries += len(acceptance)
        selected = local if case['scheme'] == 'B0_raw' else acceptance
        selected_stats = summarise(selected)
        launch_median = float(selected_stats['median'])
        launch_medians.append(launch_median)
        launch_qps.append(float(obj['timing']['throughput_qps']))
        first_query.append(float(obj['timing']['first_query']['local_query_ns' if case['scheme'] == 'B0_raw' else 'acceptance_ns']))
        for phase, values in obj['phases']['raw_ns'].items():
            phase_values[phase].extend((int(v) for v in values))
        cycle = obj['resources']['cycle_counter'].get('per_query')
        source = str(obj['resources']['cycle_counter'].get('source'))
        if cycle is not None:
            launch_cycles.append(float(cycle))
            cycle_sources.append(source)
        energy = obj['resources']['energy'].get('joules_per_query')
        if energy is not None:
            observed_energy.append(float(energy))
            if obj['resources']['energy'].get('publication_valid'):
                valid_energy.append(float(energy))
        launch_alloc.append(float(obj['operations']['observed']['python_visible_allocated_bytes_per_query']))
        peak = obj['resources'].get('peak_rss_kib')
        if peak is not None:
            launch_peak_rss.append(float(peak))
        operation_hashes.add(int(obj['operations']['observed']['hash_calls_per_query']))
        signature_ops.add(int(obj['operations']['observed']['signature_verifications_per_query']))
        launch_rows.append({'case_id': case['case_id'], 'block_id': case['block_id'], 'trial': case['trial'], **config, 'latency_basis': 'local_query_ns' if case['scheme'] == 'B0_raw' else 'acceptance_ns', 'measured_queries': int(case['measured_queries']), 'warmup_queries': int(case['warmup_queries']), 'phase_queries': int(case['phase_queries']), 'query_pool_size': int(case['query_pool_size']), 'median_ns': launch_median, 'p95_ns': selected_stats['p95'], 'p99_ns': selected_stats['p99'], 'throughput_qps': obj['timing']['throughput_qps'], 'cycle_source': source, 'cycles_or_tsc_per_query': cycle, 'joules_per_query': energy, 'energy_publication_valid': obj['resources']['energy'].get('publication_valid'), 'hash_calls_per_query': obj['operations']['observed']['hash_calls_per_query'], 'allocated_bytes_per_query': obj['operations']['observed']['python_visible_allocated_bytes_per_query'], 'peak_rss_kib': peak, 'hostname': obj['system'].get('hostname')})
    selected_pooled = pooled_local if config['scheme'] == 'B0_raw' else pooled_acceptance
    selected_stats = summarise(selected_pooled)
    local_stats = selected_stats if config['scheme'] == 'B0_raw' else summarise(pooled_local)
    acceptance_stats = summarise(pooled_acceptance)
    ci_low, ci_high = bootstrap_ci(launch_medians, statistic='mean', resamples=5000, seed=int(_group_id(key), 16))
    qps_low, qps_high = bootstrap_ci(launch_qps, statistic='mean', resamples=5000, seed=int(_group_id(key)[::-1], 16))
    row = {'group_id': _group_id(key), **config, 'launches': len(paths), 'measured_queries_total': total_queries, 'latency_basis': 'local_query_ns' if config['scheme'] == 'B0_raw' else 'acceptance_ns', 'median_ns': selected_stats['median'], 'p95_ns': selected_stats['p95'], 'p99_ns': selected_stats['p99'], 'mean_ns': selected_stats['mean'], 'launch_median_mean_ns': statistics.fmean(launch_medians), 'launch_median_ci95_low_ns': ci_low, 'launch_median_ci95_high_ns': ci_high, 'local_query_median_ns': local_stats['median'], 'acceptance_median_ns': acceptance_stats['median'], 'first_query_median_ns': percentile(first_query, 50), 'throughput_mean_qps': statistics.fmean(launch_qps), 'throughput_ci95_low_qps': qps_low, 'throughput_ci95_high_qps': qps_high}
    phase_rows: list[dict[str, Any]] = []
    for phase, values in sorted(phase_values.items()):
        phase_stats = summarise(values)
        phase_rows.append({'group_id': row['group_id'], **config, 'phase': phase, 'samples': len(values), 'median_ns': phase_stats['median'], 'p95_ns': phase_stats['p95'], 'p99_ns': phase_stats['p99'], 'mean_ns': phase_stats['mean']})
    cycle_source = ';'.join(sorted(set(cycle_sources))) if cycle_sources else 'unavailable'
    cycle_mean = statistics.fmean(launch_cycles) if launch_cycles else None
    energy_mean_valid = statistics.fmean(valid_energy) if valid_energy else None
    energy_mean_observed = statistics.fmean(observed_energy) if observed_energy else None
    resource_row = {'group_id': row['group_id'], **config, 'launches': len(paths), 'throughput_mean_qps': statistics.fmean(launch_qps), 'throughput_ci95_low_qps': qps_low, 'throughput_ci95_high_qps': qps_high, 'cycle_counter_source': cycle_source, 'cycles_or_tsc_per_query_mean': cycle_mean, 'energy_valid_launches': len(valid_energy), 'joules_per_query_mean_valid': energy_mean_valid, 'joules_per_query_mean_observed_nonexclusive': energy_mean_observed, 'hash_calls_per_query': min(operation_hashes) if len(operation_hashes) == 1 else None, 'signature_verifications_per_query': min(signature_ops) if len(signature_ops) == 1 else None, 'python_visible_allocated_bytes_per_query_median': percentile(launch_alloc, 50), 'peak_rss_kib_median': percentile(launch_peak_rss, 50) if launch_peak_rss else None}
    return (row, phase_rows, [resource_row], launch_rows)

def _index_rows(rows: Iterable[dict[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple((row[field] for field in GROUP_FIELDS)): row for row in rows}

def _phase_lookup(phase_rows: list[dict[str, Any]]) -> dict[tuple[tuple[Any, ...], str], dict[str, Any]]:
    return {(tuple((row[field] for field in GROUP_FIELDS)), row['phase']): row for row in phase_rows}

def _make_models(latency_rows: list[dict[str, Any]], phase_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    n_rows = [row for row in latency_rows if row['sweep'] == 'n_scaling' and row['ablation'] == 'safe' and (row['layout'] == 'epoch_packed') and (row['codec'] == 'raw-v1') and (row['block_bytes'] == 2048)]
    for scheme in ('B2_full', 'B4_ext'):
        selected = sorted([row for row in n_rows if row['scheme'] == scheme], key=lambda row: row['epoch_length'])
        xs = [math.log2(1 << (int(row['epoch_length']) - 1).bit_length()) for row in selected]
        ys = [float(row['median_ns']) for row in selected]
        fit = _simple_fit(xs, ys)
        models.append({'model': f'{scheme}: T = a + b*log2(n_prime)', 'response': 'median acceptance latency (ns)', 'predictor_1': 'log2(n_prime)', 'predictor_2': '', **fit})
    leaf = sorted([row for row in n_rows if row['scheme'] == 'B3_leaf'], key=lambda row: row['epoch_length'])
    leaf_x = []
    leaf_y = []
    for row in leaf:
        n_prime = 1 << (int(row['epoch_length']) - 1).bit_length()
        leaf_x.append((float(n_prime), math.log2(n_prime)))
        leaf_y.append(float(row['median_ns']))
    fit_leaf = _multiple_fit(leaf_x, leaf_y)
    models.append({'model': 'B3_leaf: T = a + b*n_prime + c*log2(n_prime)', 'response': 'median acceptance latency (ns)', 'predictor_1': 'n_prime', 'predictor_2': 'log2(n_prime)', **fit_leaf})
    phase_index = _phase_lookup(phase_rows)
    leaf_resolve_x: list[tuple[float, float]] = []
    leaf_resolve_y: list[float] = []
    for row in leaf:
        key = tuple((row[field] for field in GROUP_FIELDS))
        phase = phase_index.get((key, 'resolve_witness'))
        if phase:
            n_prime = 1 << (int(row['epoch_length']) - 1).bit_length()
            leaf_resolve_x.append((float(n_prime), math.log2(n_prime)))
            leaf_resolve_y.append(float(phase['median_ns']))
    fit_resolve = _multiple_fit(leaf_resolve_x, leaf_resolve_y)
    models.append({'model': 'B3_leaf ResolveWitness = a + b*n_prime + c*log2(n_prime)', 'response': 'median ResolveWitness latency (ns)', 'predictor_1': 'n_prime', 'predictor_2': 'log2(n_prime)', **fit_resolve})
    history = sorted([row for row in latency_rows if row['sweep'] == 'history_depth' and row['scheme'] == 'B2_full' and (row['ablation'] == 'safe')], key=lambda row: row['historical_epochs'])
    for phase_name in ('check_resp', 'verify_anchor', 'lookup_msi'):
        xs: list[float] = []
        ys: list[float] = []
        for row in history:
            key = tuple((row[field] for field in GROUP_FIELDS))
            phase = phase_index.get((key, phase_name))
            if phase:
                xs.append(math.log10(float(row['historical_epochs'])))
                ys.append(float(phase['median_ns']))
        fit = _simple_fit(xs, ys)
        models.append({'model': f'history {phase_name} = a + b*log10(T_s)', 'response': f'median {phase_name} latency (ns)', 'predictor_1': 'log10(T_s)', 'predictor_2': '', **fit})
    return models

def _model_by_prefix(models: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    for row in models:
        if str(row.get('model', '')).startswith(prefix):
            return row
    return None

def _make_hypotheses(latency_rows: list[dict[str, Any]], phase_rows: list[dict[str, Any]], models: list[dict[str, Any]], resource_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    n_rows = [row for row in latency_rows if row['sweep'] == 'n_scaling' and row['ablation'] == 'safe']
    by_scheme_n = {(row['scheme'], row['epoch_length']): row for row in n_rows}
    common_n = sorted(set((n for scheme, n in by_scheme_n if scheme == 'B2_full')) & set((n for scheme, n in by_scheme_n if scheme == 'B4_ext')))
    ratios = []
    for n in common_n:
        full = float(by_scheme_n['B2_full', n]['median_ns'])
        ext = float(by_scheme_n['B4_ext', n]['median_ns'])
        ratios.append(max(full / ext, ext / full))
    full_model = _model_by_prefix(models, 'B2_full:')
    ext_model = _model_by_prefix(models, 'B4_ext:')
    exact_full_ext_hashes = all((row.get('hash_calls_per_query') == (1 << (int(row['epoch_length']) - 1).bit_length()).bit_length() for row in resource_rows if row['sweep'] == 'n_scaling' and row['scheme'] in {'B2_full', 'B4_ext'}))
    h2a = bool(ratios and max(ratios) <= 1.5 and exact_full_ext_hashes and full_model and ext_model and (full_model.get('status') == 'ok') and (ext_model.get('status') == 'ok') and (float(full_model.get('slope', -1)) >= 0) and (float(ext_model.get('slope', -1)) >= 0))
    checks.append({'hypothesis': 'H2a', 'status': 'PASS' if h2a else 'NOT_EVALUATED' if not common_n else 'FAIL', 'criterion': 'B2/B4 exact depth+1 hash counts, non-negative log-depth slopes, and worst symmetric median-latency ratio <= 1.5', 'observed': json.dumps({'common_n_points': len(common_n), 'worst_symmetric_ratio': max(ratios) if ratios else None, 'hash_counts_exact': exact_full_ext_hashes, 'B2_slope_ns_per_depth': full_model.get('slope') if full_model else None, 'B4_slope_ns_per_depth': ext_model.get('slope') if ext_model else None}, sort_keys=True)})
    leaf_model = _model_by_prefix(models, 'B3_leaf ResolveWitness')
    leaf_resources = [row for row in resource_rows if row['sweep'] == 'n_scaling' and row['scheme'] == 'B3_leaf']
    leaf_hash_exact = all((row.get('hash_calls_per_query') == (1 << (int(row['epoch_length']) - 1).bit_length()) + (1 << (int(row['epoch_length']) - 1).bit_length()).bit_length() for row in leaf_resources))
    phase_index = _phase_lookup(phase_rows)
    common_leaf_full = sorted(set((n for scheme, n in by_scheme_n if scheme == 'B2_full')) & set((n for scheme, n in by_scheme_n if scheme == 'B3_leaf')))
    localization = None
    chosen_n = None
    if common_leaf_full:
        chosen_n = max(common_leaf_full)
        full_row = by_scheme_n['B2_full', chosen_n]
        leaf_row = by_scheme_n['B3_leaf', chosen_n]
        full_key = tuple((full_row[f] for f in GROUP_FIELDS))
        leaf_key = tuple((leaf_row[f] for f in GROUP_FIELDS))
        full_resolve = phase_index.get((full_key, 'resolve_witness'))
        leaf_resolve = phase_index.get((leaf_key, 'resolve_witness'))
        total_delta = float(leaf_row['median_ns']) - float(full_row['median_ns'])
        resolve_delta = float(leaf_resolve['median_ns']) - float(full_resolve['median_ns']) if full_resolve and leaf_resolve else math.nan
        if total_delta > 0 and math.isfinite(resolve_delta):
            localization = resolve_delta / total_delta
    h2b = bool(leaf_resources and leaf_hash_exact and leaf_model and (leaf_model.get('status') == 'ok') and (float(leaf_model.get('coefficient_1', -1)) > 0) and (float(leaf_model.get('r_squared', 0)) >= 0.9) and (localization is not None) and (localization >= 0.75))
    checks.append({'hypothesis': 'H2b', 'status': 'PASS' if h2b else 'NOT_EVALUATED' if not leaf_resources else 'FAIL', 'criterion': 'B3 exact n_prime+depth+1 hash count, positive linear n_prime coefficient with R^2 >= 0.90, and >=75% of B3-vs-B2 latency delta localised to ResolveWitness', 'observed': json.dumps({'hash_counts_exact': leaf_hash_exact, 'resolve_n_prime_coefficient': leaf_model.get('coefficient_1') if leaf_model else None, 'resolve_model_r2': leaf_model.get('r_squared') if leaf_model else None, 'localization_n': chosen_n, 'resolve_delta_fraction': localization}, sort_keys=True)})
    history = [row for row in latency_rows if row['sweep'] == 'history_depth' and row['scheme'] == 'B2_full']
    relative_ranges: dict[str, float | None] = {}
    for phase_name in ('check_resp', 'verify_anchor'):
        values = []
        for row in history:
            key = tuple((row[f] for f in GROUP_FIELDS))
            phase = phase_index.get((key, phase_name))
            if phase:
                values.append(float(phase['median_ns']))
        if values and statistics.median(values) > 0:
            relative_ranges[phase_name] = (max(values) - min(values)) / statistics.median(values)
        else:
            relative_ranges[phase_name] = None
    h2c = bool(history and all((v is not None and v <= 0.25 for v in relative_ranges.values())))
    checks.append({'hypothesis': 'H2c', 'status': 'PASS' if h2c else 'NOT_EVALUATED' if not history else 'FAIL', 'criterion': 'Across T_s=10^2..10^5, median CheckResp and VerifyAnchor relative ranges are each <=25%', 'observed': json.dumps(relative_ranges, sort_keys=True)})
    ablation_rows = [row for row in latency_rows if row['sweep'] == 'ablations']
    overheads: dict[str, list[float]] = defaultdict(list)
    for scheme in ('B2_full', 'B3_leaf', 'B4_ext'):
        rows = {row['ablation']: row for row in ablation_rows if row['scheme'] == scheme}
        if 'safe' in rows and 'no_checkresp' in rows:
            overheads['check_resp'].append(float(rows['safe']['median_ns']) - float(rows['no_checkresp']['median_ns']))
        if 'safe' in rows and 'cached_anchor' in rows:
            overheads['direct_anchor'].append(float(rows['safe']['median_ns']) - float(rows['cached_anchor']['median_ns']))
    checks.append({'hypothesis': 'E2-overhead-ablation', 'status': 'PASS' if overheads and all((v >= 0 for values in overheads.values() for v in values)) else 'NOT_EVALUATED' if not overheads else 'FAIL', 'criterion': 'Safe pipeline median is not faster than bypassing CheckResp or using a cached valid anchor result', 'observed': json.dumps(overheads, sort_keys=True)})
    return checks

def _make_pairwise(launch_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_block: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for row in launch_rows:
        by_block[str(row['block_id'])][str(row['scheme']), str(row['ablation'])] = row
    comparisons = [('B3_leaf', 'safe', 'B2_full', 'safe', 'B3-B2'), ('B4_ext', 'safe', 'B2_full', 'safe', 'B4-B2'), ('B1_verified', 'safe', 'B2_full', 'safe', 'B1-B2'), ('B0_raw', 'safe', 'B2_full', 'safe', 'B0-B2'), ('B2_full', 'safe', 'B2_full', 'no_checkresp', 'safe-no_checkresp'), ('B2_full', 'safe', 'B2_full', 'cached_anchor', 'safe-cached_anchor'), ('B3_leaf', 'safe', 'B3_leaf', 'no_checkresp', 'safe-no_checkresp'), ('B3_leaf', 'safe', 'B3_leaf', 'cached_anchor', 'safe-cached_anchor'), ('B4_ext', 'safe', 'B4_ext', 'no_checkresp', 'safe-no_checkresp'), ('B4_ext', 'safe', 'B4_ext', 'cached_anchor', 'safe-cached_anchor')]
    grouped: dict[tuple[Any, ...], tuple[list[float], list[float]]] = {}
    for block_id, entries in by_block.items():
        sample_row = next(iter(entries.values()))
        for a_scheme, a_ab, b_scheme, b_ab, label in comparisons:
            a = entries.get((a_scheme, a_ab))
            b = entries.get((b_scheme, b_ab))
            if not a or not b:
                continue
            factor_key = (sample_row['sweep'], sample_row['epoch_length'], sample_row['historical_epochs'], sample_row['block_bytes'], sample_row['layout'], sample_row['codec'], label, a_scheme, b_scheme)
            if factor_key not in grouped:
                grouped[factor_key] = ([], [])
            grouped[factor_key][0].append(float(a['median_ns']))
            grouped[factor_key][1].append(float(b['median_ns']))
    rows: list[dict[str, Any]] = []
    for key, (xs, ys) in sorted(grouped.items(), key=lambda item: str(item[0])):
        point, lo, hi = paired_bootstrap_ci(xs, ys, resamples=5000, seed=0)
        sweep, n, t, block_bytes, layout, codec, label, a_scheme, b_scheme = key
        rows.append({'sweep': sweep, 'epoch_length': n, 'historical_epochs': t, 'block_bytes': block_bytes, 'layout': layout, 'codec': codec, 'comparison': label, 'configuration_a': a_scheme, 'configuration_b': b_scheme, 'paired_launches': len(xs), 'mean_paired_difference_ns_a_minus_b': point, 'ci95_low_ns': lo, 'ci95_high_ns': hi, 'cliffs_delta_launch_medians': cliffs_delta(xs, ys)})
    return rows

def _make_figures(output: Path, latency_rows: list[dict[str, Any]], phase_rows: list[dict[str, Any]]) -> None:
    n_rows = [row for row in latency_rows if row['sweep'] == 'n_scaling' and row['ablation'] == 'safe' and (row['block_bytes'] == 2048) and (row['layout'] == 'epoch_packed') and (row['codec'] == 'raw-v1')]
    series: dict[str, list[tuple[float, float]]] = {}
    ticks: dict[float, str] = {}
    for scheme in ('B0_raw', 'B1_verified', 'B2_full', 'B3_leaf', 'B4_ext'):
        points = []
        for row in sorted([r for r in n_rows if r['scheme'] == scheme], key=lambda r: r['epoch_length']):
            n = int(row['epoch_length'])
            n_prime = 1 << (n - 1).bit_length()
            x = math.log2(n_prime)
            y_us = float(row['median_ns']) / 1000.0
            points.append((x, y_us))
            ticks[x] = str(n_prime)
        if points:
            series[SCHEME_LABELS[scheme]] = points
    line_chart(output / 'fig_E2_latency_scaling.svg', title='E2 Local Verified-Query Latency Scaling', x_label='log2(padded epoch length n′); tick labels show n′', y_label='Median latency (microseconds, log scale)', series=series, x_tick_labels=sorted(ticks.items()), log_y=True)
    phase_index = _phase_lookup(phase_rows)
    candidates = sorted({int(r['epoch_length']) for r in n_rows if int(r['epoch_length']) <= 4096})
    chosen_n = max(candidates) if candidates else None
    categories: list[str] = []
    phase_values: dict[str, list[float]] = {'CheckResp': [], 'Decode': [], 'ResolveWitness': [], 'VerifyMember': [], 'VerifyAnchor': []}
    if chosen_n is not None:
        for scheme in ('B1_verified', 'B2_full', 'B3_leaf', 'B4_ext'):
            matching = [r for r in n_rows if r['scheme'] == scheme and int(r['epoch_length']) == chosen_n]
            if not matching:
                continue
            row = matching[0]
            key = tuple((row[f] for f in GROUP_FIELDS))
            mapping = {'CheckResp': 'check_resp', 'Decode': 'decode_payload', 'ResolveWitness': 'resolve_witness', 'VerifyMember': 'verify_member', 'VerifyAnchor': 'verify_anchor'}
            if not all(((key, p) in phase_index for p in mapping.values())):
                continue
            categories.append(SCHEME_LABELS[scheme].replace('LocalArchive-', 'LA-').replace('MSI-', ''))
            for label, phase in mapping.items():
                phase_values[label].append(float(phase_index[key, phase]['median_ns']) / 1000.0)
    stacked_bar_chart(output / 'fig_E2_phase_breakdown.svg', title=f"E2 Median Phase Breakdown (n={(chosen_n if chosen_n is not None else 'NA')})", y_label='Instrumented phase median (microseconds)', categories=categories, phase_values=phase_values)
    history = sorted([r for r in latency_rows if r['sweep'] == 'history_depth' and r['scheme'] == 'B2_full'], key=lambda r: r['historical_epochs'])
    hist_series: dict[str, list[tuple[float, float]]] = {'CheckResp': [], 'VerifyAnchor': [], 'MSI lookup': []}
    hist_ticks: list[tuple[float, str]] = []
    for row in history:
        t = int(row['historical_epochs'])
        x = math.log10(t)
        hist_ticks.append((x, f'{t:g}'))
        key = tuple((row[f] for f in GROUP_FIELDS))
        for label, phase in (('CheckResp', 'check_resp'), ('VerifyAnchor', 'verify_anchor'), ('MSI lookup', 'lookup_msi')):
            p = phase_index.get((key, phase))
            if p:
                hist_series[label].append((x, float(p['median_ns']) / 1000.0))
    line_chart(output / 'fig_E2_history_depth.svg', title='E2 Phase Sensitivity to Historical Depth', x_label='log10(T_s); tick labels show historical epochs', y_label='Median phase latency (microseconds)', series={k: v for k, v in hist_series.items() if v}, x_tick_labels=hist_ticks, log_y=False)

def _write_manifest(output: Path) -> None:
    entries = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != 'manifest.sha256':
            entries.append(f'{sha256_file(path)}  {path.name}')
    atomic_write_text(output / 'manifest.sha256', '\n'.join(entries) + '\n')

def analyze(raw_dir: Path, output: Path, plan_path: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    blocks = read_plan(plan_path)
    expected: dict[str, dict[str, Any]] = {str(case['case_id']): case for block in blocks for case in block['cases']}
    paths_by_id: dict[str, Path] = {}
    invalid: list[str] = []
    internal_checks_pass = True
    hosts: set[str] = set()
    for path in sorted(raw_dir.glob('*.json.gz')):
        try:
            obj = read_json(path)
            case_id = str(obj['case']['case_id'])
            if obj.get('schema_version') != 1 or obj.get('experiment') != 'E2':
                invalid.append(path.name)
                continue
            if case_id in paths_by_id:
                raise RuntimeError(f'duplicate raw case_id {case_id}')
            paths_by_id[case_id] = path
            internal_checks_pass = internal_checks_pass and all(obj.get('checks', {}).values())
            hosts.add(str(obj.get('system', {}).get('hostname', 'unknown')))
        except Exception:
            invalid.append(path.name)
    missing = sorted(set(expected) - set(paths_by_id))
    extra = sorted(set(paths_by_id) - set(expected))
    if missing or extra or invalid:
        raise RuntimeError(f'raw result set is incomplete or invalid: missing={len(missing)}, extra={len(extra)}, invalid={len(invalid)}; missing_sample={missing[:5]}, extra_sample={extra[:5]}, invalid_sample={invalid[:5]}')
    grouped: dict[tuple[Any, ...], list[Path]] = defaultdict(list)
    for case_id, path in paths_by_id.items():
        grouped[_group_key(expected[case_id])].append(path)
    latency_rows: list[dict[str, Any]] = []
    phase_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    launch_rows: list[dict[str, Any]] = []
    for key, paths in sorted(grouped.items(), key=lambda item: str(item[0])):
        latency, phases, resources, launches = _aggregate_group(paths, key)
        latency_rows.append(latency)
        phase_rows.extend(phases)
        resource_rows.extend(resources)
        launch_rows.extend(launches)
    models = _make_models(latency_rows, phase_rows)
    hypotheses = _make_hypotheses(latency_rows, phase_rows, models, resource_rows)
    pairwise = _make_pairwise(launch_rows)
    _write_csv(output / 'latency_summary.csv', latency_rows)
    _write_csv(output / 'phase_summary.csv', phase_rows)
    _write_csv(output / 'throughput_resources.csv', resource_rows)
    _write_csv(output / 'launch_summary.csv', launch_rows)
    _write_csv(output / 'model_fits.csv', models)
    _write_csv(output / 'hypothesis_checks.csv', hypotheses)
    _write_csv(output / 'pairwise_effects.csv', pairwise)
    _make_figures(output, latency_rows, phase_rows)
    statuses = {row['hypothesis']: row['status'] for row in hypotheses}
    total_queries = sum((int(row['measured_queries_total']) for row in latency_rows))
    publication_launch_requirement = min((int(row['launches']) for row in latency_rows)) >= 10
    publication_query_requirement = all((int(row['measured_queries']) >= 10000 for row in launch_rows))
    valid_energy_rows = sum((1 for row in resource_rows if int(row['energy_valid_launches']) > 0))
    cycle_sources = sorted({str(row['cycle_counter_source']) for row in resource_rows})
    plan_sha = sha256_file(plan_path)
    summary = {'schema_version': 1, 'experiment': 'E2', 'plan_audit': {'complete': True, 'plan_sha256': plan_sha, 'expected_blocks': len(blocks), 'expected_cases': len(expected), 'actual_cases': len(paths_by_id), 'missing_case_ids': [], 'extra_case_ids': [], 'invalid_files': []}, 'configuration_groups': len(latency_rows), 'total_measured_queries': total_queries, 'hosts': sorted(hosts), 'internal_checks_pass': internal_checks_pass, 'publication_protocol': {'at_least_10_independent_launches_per_configuration': publication_launch_requirement, 'at_least_10000_measured_queries_in_every_launch': publication_query_requirement, 'note': 'Both requirements are audited from the actual raw launch records, not inferred from a configuration label.'}, 'hypothesis_status': statuses, 'all_primary_hypotheses_pass': all((statuses.get(h) == 'PASS' for h in ('H2a', 'H2b', 'H2c'))), 'cycle_counter_sources': cycle_sources, 'resource_groups_with_publication_valid_energy': valid_energy_rows, 'energy_note': 'A valid RAPL value also requires E2_ENERGY_EXCLUSIVE=1 and an actually exclusive job. HPC server energy must not be relabelled as edge-platform energy.'}
    atomic_write_json(output / 'summary.json', summary)
    report_lines = ['# Experiment E2: Local Verified-Query Computation', '', '## Completeness', '', f'- Plan blocks: **{len(blocks)}**', f'- Independent process-launch results: **{len(paths_by_id)}**', f'- Aggregated configuration groups: **{len(latency_rows)}**', f'- Measured valid queries: **{total_queries:,}**', f"- Raw-result internal checks: **{('PASS' if internal_checks_pass else 'FAIL')}**", f"- Publication launch-count requirement: **{('PASS' if publication_launch_requirement else 'NOT MET')}**", f"- Publication per-launch query requirement: **{('PASS' if publication_query_requirement else 'NOT MET')}**", '', '## Pre-registered hypotheses', '']
    for row in hypotheses:
        report_lines.extend([f"### {row['hypothesis']}: {row['status']}", '', f"Criterion: {row['criterion']}", '', f"Observed: `{row['observed']}`", ''])
    report_lines.extend(['## Interpretation boundaries', '', 'The primary latency excludes network and disk retrieval. B0 uses local-query latency because it has no cryptographic acceptance phase; B1-B4 use acceptance latency after the deterministic in-process handoff. Phase timings are collected in a separate sampled pass and therefore explain localisation, not an exact additive reconstruction of the primary distribution.', '', "Hardware CPU cycles are reported only when Linux perf_event access succeeds. Otherwise x86 TSC ticks are labelled as such and must not be called core cycles. Energy is publication-valid only when a readable package-energy interface is combined with a genuinely exclusive job and the explicit E2_ENERGY_EXCLUSIVE=1 assertion. A La Trobe server result does not replace the redesigned chapter's edge-device energy measurement.", '', '## Generated artifacts', '', '- `latency_summary.csv`: pooled median, p95, p99 and launch-level confidence intervals.', '- `phase_summary.csv`: CheckResp, decode, ResolveWitness, VerifyMember and VerifyAnchor phase distributions.', '- `throughput_resources.csv`: throughput, cycles/ticks, hashes, allocation volume, RSS and energy.', '- `model_fits.csv`: pre-specified logarithmic and linear-plus-log models.', '- `hypothesis_checks.csv`: machine-readable H2a-H2c decisions.', '- `pairwise_effects.csv`: paired bootstrap differences and Cliff delta.', '- `fig_E2_latency_scaling.svg`, `fig_E2_phase_breakdown.svg`, and `fig_E2_history_depth.svg`.', ''])
    atomic_write_text(output / 'report.md', '\n'.join(report_lines))
    _write_manifest(output)
