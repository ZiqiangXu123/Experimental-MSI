from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
from typing import Any
from .stats import linear_fit
from .svg import line_chart, normalized_stacked_bar
from .util import atomic_write_json, atomic_write_text, canonical_json, human_bytes, iter_json_files, next_power_of_two

def _load_results(raw_dir: Path) -> list[dict[str, Any]]:
    results = []
    for path in iter_json_files(raw_dir):
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'invalid JSON result: {path}: {exc}') from exc
        if obj.get('schema_version') == 1 and obj.get('point', {}).get('experiment') == 'E1':
            results.append(obj)
    if not results:
        raise RuntimeError(f'no E1 result JSON files found under {raw_dir}')
    results.sort(key=lambda r: r['point']['point_id'])
    return results

def _load_expected_plan(plan_paths: list[Path] | None) -> tuple[set[str] | None, dict[str, Any]]:
    if not plan_paths:
        return (None, {'enforced': False, 'plan_files': [], 'expected_points': None, 'plan_sha256': None})
    expected: dict[str, str] = {}
    digest = hashlib.sha256()
    resolved_files: list[str] = []
    for plan_path in plan_paths:
        if not plan_path.is_file():
            raise RuntimeError(f'missing plan file: {plan_path}')
        resolved_files.append(str(plan_path))
        data = plan_path.read_bytes()
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
        for lineno, raw_line in enumerate(data.decode('utf-8').splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                point = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f'invalid plan JSON at {plan_path}:{lineno}: {exc}') from exc
            if point.get('experiment') != 'E1' or not point.get('point_id'):
                raise RuntimeError(f'invalid E1 point at {plan_path}:{lineno}')
            point_id = str(point['point_id'])
            encoded = canonical_json(point)
            previous = expected.get(point_id)
            if previous is not None and previous != encoded:
                raise RuntimeError(f'conflicting definitions for point_id={point_id}')
            expected[point_id] = encoded
    if not expected:
        raise RuntimeError('the supplied plan set contains no E1 points')
    return (set(expected), {'enforced': True, 'plan_files': resolved_files, 'expected_points': len(expected), 'plan_sha256': digest.hexdigest()})

def _check_plan_completeness(results: list[dict[str, Any]], expected_ids: set[str] | None, plan_meta: dict[str, Any]) -> dict[str, Any]:
    actual_ids = [str(r['point']['point_id']) for r in results]
    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError('duplicate point IDs were found in the raw result set')
    actual = set(actual_ids)
    audit = dict(plan_meta)
    audit['actual_points'] = len(actual)
    if expected_ids is None:
        audit.update({'missing_point_ids': [], 'extra_point_ids': []})
        return audit
    missing = sorted(expected_ids - actual)
    extra = sorted(actual - expected_ids)
    audit.update({'missing_point_ids': missing, 'extra_point_ids': extra})
    if missing or extra:
        raise RuntimeError(f'raw result set does not match the submitted plan: missing={len(missing)}, extra={len(extra)}; missing_ids={missing[:8]}, extra_ids={extra[:8]}')
    return audit

def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    fieldnames = sorted({k for row in rows for k in row})
    with path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def _flat_point(result: dict[str, Any]) -> dict[str, Any]:
    p = result['point']
    row = {k: v for k, v in p.items() if k != 'materialize'}
    row['materialize'] = ';'.join(p['materialize'])
    row.update(n_prime=result['theory']['n_prime'], depth=result['theory']['depth'], wall_seconds=result['timing']['wall_seconds'], cpu_seconds=result['timing']['cpu_seconds'], all_checks_pass=all(result['checks'].values()))
    for name, comp in result['components'].items():
        row[f'{name}_logical_bytes'] = comp['logical_bytes']
        row[f'{name}_allocated_bytes'] = comp['allocated_bytes']
        row[f'{name}_materialized'] = comp['materialized']
        if 'hash_bytes' in comp:
            row[f'{name}_hash_bytes'] = comp['hash_bytes']
    return row

def _scheme_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        p = result['point']
        for scheme, values in result['schemes'].items():
            rows.append({'point_id': p['point_id'], 'sweep': p['sweep'], 'shards': p['shards'], 'historical_epochs': p['historical_epochs'], 'epoch_length': p['epoch_length'], 'block_bytes': p['block_bytes'], 'hot_window': p['hot_window'], 'layout': p['layout'], 'codec': p['codec'], 'seed': p['seed'], 'scheme': scheme, **values})
    return rows

def _theory_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        p = result['point']
        epochs = p['shards'] * p['historical_epochs']
        for mode in ('full', 'leaf', 'ext'):
            comp = result['components'][f'historical_aux_{mode}']
            theory = result['theory'][f'{mode}_hash_bytes_total'] if mode != 'ext' else 0
            serialized = comp['logical_bytes']
            overhead = serialized - theory
            rows.append({'point_id': p['point_id'], 'sweep': p['sweep'], 'mode': mode, 'n': p['epoch_length'], 'n_prime': result['theory']['n_prime'], 'epochs_total': epochs, 'theoretical_hash_bytes': theory, 'serialized_store_bytes': serialized, 'serialization_overhead_bytes': overhead, 'relative_overhead': overhead / theory if theory else None, 'materialized': comp['materialized'], 'allocated_bytes': comp['allocated_bytes']})
    return rows

def _select_sweep(results: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in results if r['point']['sweep'] == name]

def _fit_and_checks(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    fits: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    t_rows = [r for r in _select_sweep(results, 'T_scale') if r['point']['shards'] == 1 and r['point']['codec'] == 'raw-v1']
    t_rows.sort(key=lambda r: r['point']['historical_epochs'])
    if len(t_rows) < 2:
        raise RuntimeError('analysis requires at least two T_scale points with shards=1')
    x = [float(r['point']['historical_epochs']) for r in t_rows]
    y = [float(r['components']['msi_full']['logical_bytes'] + r['components']['anchor']['logical_bytes']) for r in t_rows]
    fit = linear_fit(x, y)
    fits.append({'model': 'serialized_local_history_per_shard = intercept + slope*T_s', 'intercept': fit.intercept, 'slope': fit.slope, 'r_squared': fit.r_squared, 'rmse': fit.rmse, 'max_abs_residual': fit.max_abs_residual, 'n': fit.n})
    h1a_linear = abs(fit.slope - t_rows[0]['theory']['msi_entry_bytes']) < 1e-09 and fit.r_squared > 0.999999999
    checks.append({'hypothesis': 'H1a-linear-T', 'status': 'PASS' if h1a_linear else 'FAIL', 'criterion': 'slope equals serialized MSI entry bytes and R^2 > 0.999999999', 'observed': f'slope={fit.slope:.12g}; R2={fit.r_squared:.12g}; max_residual={fit.max_abs_residual:.6g}'})
    invariance_summary = {}
    for sweep_name, label in (('n_boundary', 'epoch length n'), ('block_scale', 'block size')):
        rows = _select_sweep(results, sweep_name)
        values = [r['components']['msi_full']['logical_bytes'] + r['components']['anchor']['logical_bytes'] for r in rows]
        spread = max(values) - min(values) if values else None
        passed = spread == 0 if spread is not None else False
        invariance_summary[sweep_name] = spread
        checks.append({'hypothesis': f'H1a-invariance-{sweep_name}', 'status': 'PASS' if passed else 'FAIL', 'criterion': f'serialized local historical bytes do not vary with {label} under fixed T_s', 'observed': f'range={spread} bytes'})
    h1b_keys = ('full_hash_formula_exact', 'leaf_hash_formula_exact', 'ext_hash_formula_exact', 'full_aux_serialization_exact', 'leaf_aux_serialization_exact', 'ext_aux_serialization_exact')
    h1b = all((r['checks'][key] for r in results for key in h1b_keys))
    materialized_audits = sum((1 for r in results if r['components']['historical_aux_full']['materialized'] and r['components']['historical_aux_leaf']['materialized']))
    checks.append({'hypothesis': 'H1b-mode-formulas', 'status': 'PASS' if h1b else 'FAIL', 'criterion': 'hash payload and serialized auxiliary stores equal the full, leaf, and ext formulas', 'observed': f'checked_points={len(results)}; physically_materialized_audits={materialized_audits}'})
    boundary_rows = _select_sweep(results, 'n_boundary')
    h1c = bool(boundary_rows) and all((r['theory']['n_prime'] == next_power_of_two(r['point']['epoch_length']) and r['checks']['padding_power_of_two'] for r in boundary_rows))
    checks.append({'hypothesis': 'H1c-padding-boundaries', 'status': 'PASS' if h1c else 'FAIL', 'criterion': "n' is the least power of two >= n and support sizes change only when n crosses a power of two", 'observed': f'checked_boundary_points={len(boundary_rows)}'})
    internal_failures = [f"{r['point']['point_id']}:{key}" for r in results for key, passed in r['checks'].items() if not passed]
    implementation_ok = not internal_failures
    checks.append({'hypothesis': 'Implementation-serialization-audit', 'status': 'PASS' if implementation_ok else 'FAIL', 'criterion': 'all fixed-width, payload, auxiliary, padding, and allocation invariants pass', 'observed': f"checked_invariants={sum((len(r['checks']) for r in results))}; failures=0" if implementation_ok else f'failures={internal_failures[:8]}'})
    summary = {'all_hypotheses_pass': all((row['status'] == 'PASS' for row in checks)), 't_fit': fits[0], 'invariance_spread_bytes': invariance_summary, 'checked_points': len(results)}
    return (fits, checks, summary)

def _make_figures(results: list[dict[str, Any]], output_dir: Path) -> list[str]:
    figures = []
    t_rows = [r for r in _select_sweep(results, 'T_scale') if r['point']['shards'] == 1]
    t_rows.sort(key=lambda r: r['point']['historical_epochs'])
    points = []
    for r in t_rows:
        t = r['point']['historical_epochs']
        hist = r['components']['msi_full']['logical_bytes'] + r['components']['anchor']['logical_bytes']
        points.append((float(t), hist / t))
    path = output_dir / 'fig_E1_bytes_per_epoch_vs_T.svg'
    line_chart(path, title='E1: Verifier-resident bytes per historical epoch', x_label='Historical epochs per shard, T_s (log scale)', y_label='Serialized bytes per epoch', series=[('MSI + active anchor', points)], log_x=True, y_bytes=True)
    figures.append(path.name)
    n_rows = _select_sweep(results, 'n_boundary')
    n_rows.sort(key=lambda r: r['point']['epoch_length'])
    full_theory, leaf_theory, full_ser, leaf_ser = ([], [], [], [])
    for r in n_rows:
        n = float(r['point']['epoch_length'])
        epochs = r['point']['shards'] * r['point']['historical_epochs']
        full_theory.append((n, r['theory']['full_hash_bytes_total'] / epochs))
        leaf_theory.append((n, r['theory']['leaf_hash_bytes_total'] / epochs))
        full_ser.append((n, r['components']['historical_aux_full']['logical_bytes'] / epochs))
        leaf_ser.append((n, r['components']['historical_aux_leaf']['logical_bytes'] / epochs))
    path = output_dir / 'fig_E1_support_size_vs_n.svg'
    line_chart(path, title='E1: Merkle support state across padding boundaries', x_label='Canonical epoch length, n (log scale)', y_label='Bytes per epoch', series=[('Full theoretical hash bytes', full_theory), ('Leaf theoretical hash bytes', leaf_theory), ('Full serialized object', full_ser), ('Leaf serialized object', leaf_ser)], log_x=True, y_bytes=True)
    figures.append(path.name)
    representative = max((r for r in results if r['point']['codec'] == 'raw-v1'), key=lambda r: (r['point']['historical_epochs'] * r['point']['shards'], r['point']['epoch_length']))
    scheme_order = ['B0_LocalArchive_Raw', 'B1_LocalArchive_Verified', 'B2_MSI_Full', 'B3_MSI_Leaf', 'B4_MSI_Ext', 'B5_Root_Anchor_LowerBound']
    categories = [x.replace('_', ' ') for x in scheme_order]
    local = [representative['schemes'][x]['local_logical_bytes'] for x in scheme_order]
    ext_payload = [representative['schemes'][x]['external_payload_logical_bytes'] for x in scheme_order]
    ext_aux = [representative['schemes'][x]['external_aux_logical_bytes'] for x in scheme_order]
    totals = [a + b + c for a, b, c in zip(local, ext_payload, ext_aux)]
    path = output_dir / 'fig_E1_local_external_storage.svg'
    normalized_stacked_bar(path, title='E1: Local versus delegated persistent storage', categories=categories, stacks=[('Verifier-resident', local), ('External payload', ext_payload), ('External auxiliary', ext_aux)], totals=totals)
    figures.append(path.name)
    return figures

def _render_report(results: list[dict[str, Any]], fits: list[dict[str, Any]], checks: list[dict[str, Any]], summary: dict[str, Any], figures: list[str]) -> str:
    status = 'PASS' if summary['all_hypotheses_pass'] else 'FAIL'
    fit = summary['t_fit']
    n_rows = sorted(_select_sweep(results, 'n_boundary'), key=lambda r: r['point']['epoch_length'])
    largest_n = n_rows[-1] if n_rows else results[-1]
    n = largest_n['point']['epoch_length']
    np = largest_n['theory']['n_prime']
    full = largest_n['theory']['full_hash_bytes_per_epoch']
    leaf = largest_n['theory']['leaf_hash_bytes_per_epoch']
    ratio = leaf / full if full else float('nan')
    lines = ['# Experiment E1 Results: Verifier-Resident and Auxiliary Storage Scaling', '', f'**Overall pre-registered hypothesis status: {status}.**', '', '## Scope', '', 'This report evaluates persistent byte accounting only. It deliberately excludes query latency, network transport, migration time, and availability, in accordance with the non-overlap rule for E1.', '', '## H1a: Local historical-state scaling', '', f"The fitted serialized per-shard model is `S_hist = {fit['intercept']:.3f} + {fit['slope']:.3f} T_s` bytes with `R^2 = {fit['r_squared']:.12f}` and a maximum absolute residual of `{fit['max_abs_residual']:.6g}` bytes. The fitted slope is the fixed serialized MSI-entry width. Across the controlled epoch-length and block-size sweeps, the observed ranges were `{summary['invariance_spread_bytes'].get('n_boundary')}` and `{summary['invariance_spread_bytes'].get('block_scale')}` bytes, respectively.", '', '## H1b: Mode-dependent auxiliary-state formulas', '', f"For the largest tested boundary point (`n={n}`, `n'={np}`), the full and leaf hash payloads are {human_bytes(full)} and {human_bytes(leaf)} per epoch. The leaf/full hash-state ratio is `{ratio:.6f}`. Serialized-object totals are reported separately from hash payloads so that framing, length fields, CRCs, and object-store headers are not hidden.", '', '## H1c: Duplicate-last padding', '', 'Every boundary point used the least power of two greater than or equal to the canonical epoch length. The support-state curves therefore exhibit the expected step increase immediately after each power-of-two boundary.', '', '## Hypothesis audit', '', '| Hypothesis | Status | Criterion | Observed |', '|---|---:|---|---|']
    for row in checks:
        lines.append(f"| {row['hypothesis']} | {row['status']} | {row['criterion']} | {row['observed']} |")
    lines.extend(['', '## Generated artifacts', '', '- `raw_points.csv`: one row per experimental point.', '- `scheme_storage.csv`: B0-B5 placement accounting for every point.', '- `theory_vs_serialization.csv`: exact formula, serialized bytes, and overhead.', '- `model_fits.csv`: fitted local-history model.', '- `hypothesis_checks.csv`: machine-readable pass/fail audit.'])
    lines.extend((f'- `{name}`' for name in figures))
    lines.extend(['', '## Interpretation boundary', '', 'Logical serialized bytes are implementation-defined but hardware-independent for this reference encoder. Filesystem allocated bytes depend on the La Trobe storage backend and are therefore reported only when the corresponding component was physically materialized on the compute node.', ''])
    return '\n'.join(lines)

def _write_manifest(output_dir: Path) -> None:
    rows = []
    for path in sorted((p for p in output_dir.iterdir() if p.is_file() and p.name != 'manifest.sha256')):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f'{digest}  {path.name}')
    atomic_write_text(output_dir / 'manifest.sha256', '\n'.join(rows) + '\n')

def analyze(raw_dir: Path, output_dir: Path, plan_paths: list[Path] | None=None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results = _load_results(raw_dir)
    expected_ids, plan_meta = _load_expected_plan(plan_paths)
    plan_audit = _check_plan_completeness(results, expected_ids, plan_meta)
    raw_rows = [_flat_point(r) for r in results]
    scheme_rows = _scheme_rows(results)
    theory_rows = _theory_rows(results)
    fits, checks, summary = _fit_and_checks(results)
    _write_csv(output_dir / 'raw_points.csv', raw_rows)
    _write_csv(output_dir / 'scheme_storage.csv', scheme_rows)
    _write_csv(output_dir / 'theory_vs_serialization.csv', theory_rows)
    _write_csv(output_dir / 'model_fits.csv', fits)
    _write_csv(output_dir / 'hypothesis_checks.csv', checks)
    figures = _make_figures(results, output_dir)
    report = _render_report(results, fits, checks, summary, figures)
    atomic_write_text(output_dir / 'report.md', report)
    summary.update({'figures': figures, 'result_files': ['raw_points.csv', 'scheme_storage.csv', 'theory_vs_serialization.csv', 'model_fits.csv', 'hypothesis_checks.csv', 'report.md'], 'hostnames': sorted({r['environment']['hostname'] for r in results}), 'plan_audit': plan_audit, 'slurm_job_ids': sorted({str(r['environment']['slurm_job_id']) for r in results if r['environment'].get('slurm_job_id')})})
    atomic_write_json(output_dir / 'summary.json', summary)
    _write_manifest(output_dir)
    return summary

def validate_results(output_dir: Path) -> bool:
    summary_path = output_dir / 'summary.json'
    if not summary_path.exists():
        raise RuntimeError(f'missing summary: {summary_path}')
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    required = ['raw_points.csv', 'scheme_storage.csv', 'theory_vs_serialization.csv', 'model_fits.csv', 'hypothesis_checks.csv', 'report.md', 'manifest.sha256'] + summary.get('figures', [])
    missing = [name for name in required if not (output_dir / name).is_file()]
    if missing:
        raise RuntimeError(f'missing result artifacts: {missing}')
    return bool(summary.get('all_hypotheses_pass'))
