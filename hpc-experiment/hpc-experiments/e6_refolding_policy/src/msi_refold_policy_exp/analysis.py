from __future__ import annotations
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence
from .catalog import load_cost_catalog, validate_cost_catalog
from .execution import inspect_raw
from .merkle import support_size_formula
from .migration import CRASH_POINTS, TRANSITIONS, applicable_faults
from .svg_base import line_chart
from .util import atomic_write_json, atomic_write_text, bootstrap_ci, csv_write, flatten_dict, jsonl_iter, linear_fit, read_json, sha256_file, summarise

def _median(values: Iterable[float | int]) -> float:
    items = [float(value) for value in values]
    return statistics.median(items) if items else math.nan

def _check(rows: list[dict[str, Any]], hypothesis: str, name: str, passed: bool | None, observed: Any, criterion: str, *, primary: bool=True) -> None:
    status = 'NOT_EVALUATED' if passed is None else 'PASS' if passed else 'FAIL'
    rows.append({'hypothesis': hypothesis, 'check': name, 'status': status, 'primary': primary, 'observed': json.dumps(observed, sort_keys=True, separators=(',', ':')) if isinstance(observed, (dict, list, tuple)) else observed, 'criterion': criterion})

def _write_manifest(output_dir: Path) -> None:
    lines = []
    for path in sorted(output_dir.rglob('*')):
        if not path.is_file() or path.name == 'manifest.sha256':
            continue
        lines.append(f'{sha256_file(path)}  {path.relative_to(output_dir).as_posix()}')
    atomic_write_text(output_dir / 'manifest.sha256', '\n'.join(lines) + '\n')

def verify_manifest(results_dir: str | Path) -> dict[str, Any]:
    root = Path(results_dir)
    manifest = root / 'manifest.sha256'
    failures = []
    checked = 0
    if not manifest.exists():
        return {'status': 'FAIL', 'checked': 0, 'failures': ['manifest.sha256 missing']}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        expected, relative = line.split('  ', 1)
        target = root / relative
        checked += 1
        if not target.exists():
            failures.append(f'missing: {relative}')
        elif sha256_file(target) != expected:
            failures.append(f'digest mismatch: {relative}')
    return {'status': 'PASS' if not failures else 'FAIL', 'checked': checked, 'failures': failures}

def _migration_launch_row(result: dict[str, Any]) -> dict[str, Any]:
    case = result['case']
    row: dict[str, Any] = {'case_id': result['case_id'], 'trial_kind': result['trial_kind'], 'transition': result['transition'], 'source_mode': result['source_mode'], 'target_mode': result['target_mode'], 'n': int(case['n']), 'n_prime': int(case['n_prime']), 'backend': case['backend'], 'launch': int(case['launch']), 'variant_label': case['variant_label'], 'layout': case['layout'], 'codec': case['codec'], 'block_bytes': int(case['block_bytes']), 'fault': case.get('fault', 'none'), 'all_checks_pass': result['all_checks_pass'], 'hostname': result.get('system', {}).get('hostname'), 'filesystem_type': result.get('filesystem', {}).get('filesystem_type'), 'mount_point': result.get('filesystem', {}).get('mount_point')}
    if result['trial_kind'] in {'migration_perf', 'equivalence_audit'}:
        migration = result['migration']
        row.update({'wall_ns': migration['wall_ns'], 'cpu_ns': migration['cpu_ns'], 'hash_leaf_calls': migration['hash_calls']['leaf'], 'hash_internal_calls': migration['hash_calls']['internal'], 'hash_total_calls': migration['hash_calls']['total'], 'bytes_read': migration['io']['bytes_read'], 'bytes_written': migration['io']['bytes_written'], 'metadata_bytes_written': migration['metadata_bytes_written'], 'total_bytes_written': migration['io']['bytes_written'] + migration['metadata_bytes_written'], 'target_serialized_bytes': migration['target_serialized_bytes'], 'temporary_disk_bytes': migration['temporary_disk_bytes'], 'write_amplification': migration['write_amplification'], 'peak_rss_kib': migration['peak_rss_kib'], 'minor_faults': migration['minor_faults'], 'major_faults': migration['major_faults'], 'voluntary_context_switches': migration['voluntary_context_switches'], 'involuntary_context_switches': migration['involuntary_context_switches'], 'energy_joules_observed_nonexclusive': migration['energy_joules_observed_nonexclusive'], 'energy_source': migration['energy_source'], 'root_equal': migration['root_equal'], 'protected_bytes_equal': migration['protected_bytes_equal'], 'allowed_metadata_difference_only': migration['allowed_metadata_difference_only'], 'recovery_source': migration['recovery_source']})
    if result['trial_kind'] == 'equivalence_audit':
        row.update({'pre_positions_checked': result['pre_query_audit']['positions_checked'], 'post_positions_checked': result['post_query_audit']['positions_checked'], 'leaf_resolver_positions_checked': result['post_query_audit'].get('exact_leaf_resolver_positions_checked', 0) + result['pre_query_audit'].get('exact_leaf_resolver_positions_checked', 0), 'accepted_relation_equivalent': result['accepted_relation_equivalent']})
    if result['trial_kind'] == 'fault_injection':
        row['fault_injected'] = result['fault_injected']
        if result['fault'] in CRASH_POINTS:
            recovery = result['recovery']
            row.update({'migration_rejected': False, 'child_exit_code': result['child_exit_code'], 'active_state_valid': recovery['active_state_valid'], 'active_mode_correct': recovery['active_mode_correct'], 'mixed_state_detected': recovery['mixed_state_detected'], 'recovery_ns': recovery['recovery_ns'], 'unreferenced_target_objects_cleaned': recovery['unreferenced_target_objects_cleaned']})
        else:
            row.update({'migration_rejected': result['migration_rejected'], 'old_mode_preserved': result['old_mode_preserved'], 'active_state_valid': result['active_state_valid'], 'mixed_state_detected': result['mixed_state_detected'], 'rejection_reason': result.get('rejection_reason', '')})
    return row

def _group_migration_perf(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row['trial_kind'] != 'migration_perf':
            continue
        key = (row['transition'], row['n'], row['n_prime'], row['backend'], row['variant_label'], row['layout'], row['codec'], row['block_bytes'])
        groups[key].append(row)
    output = []
    metrics = ('wall_ns', 'cpu_ns', 'hash_leaf_calls', 'hash_internal_calls', 'hash_total_calls', 'bytes_read', 'total_bytes_written', 'target_serialized_bytes', 'temporary_disk_bytes', 'write_amplification', 'peak_rss_kib')
    for key, members in sorted(groups.items()):
        row = {'transition': key[0], 'n': key[1], 'n_prime': key[2], 'backend': key[3], 'variant_label': key[4], 'layout': key[5], 'codec': key[6], 'block_bytes': key[7], 'launches': len(members)}
        for metric in metrics:
            values = [float(member[metric]) for member in members if member.get(metric) is not None]
            stats = summarise(values)
            row[f'{metric}_median'] = stats['median']
            row[f'{metric}_p95'] = stats['p95']
            row[f'{metric}_min'] = stats['min']
            row[f'{metric}_max'] = stats['max']
            if len(values) >= 2:
                deterministic_seed = int.from_bytes(__import__('hashlib').sha256(repr((key, metric)).encode()).digest()[:8], 'big')
                ci = bootstrap_ci(values, deterministic_seed, statistic='median', iterations=1000)
                row[f'{metric}_median_ci_low'] = ci[0]
                row[f'{metric}_median_ci_high'] = ci[1]
            else:
                row[f'{metric}_median_ci_low'] = stats['median']
                row[f'{metric}_median_ci_high'] = stats['median']
        output.append(row)
    return output

def _expected_hashes(source: str, n: int, n_prime: int) -> tuple[int, int, int]:
    if n_prime == 1:
        if source in {'full', 'ext'}:
            return (1, 0, 1)
        return (0, 0, 0)
    if source == 'ext':
        leaf = n
        internal = 3 * (n_prime - 1)
    else:
        leaf = 0
        internal = 2 * (n_prime - 1)
    return (leaf, internal, leaf + internal)

def _migration_models(summary_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        if row['variant_label'] == 'canonical':
            groups[row['transition'], row['backend']].append(row)
    for (transition, backend), members in sorted(groups.items()):
        members = sorted(members, key=lambda value: int(value['n_prime']))
        if len({int(row['n_prime']) for row in members}) < 2:
            continue
        for metric in ('wall_ns_median', 'cpu_ns_median', 'hash_total_calls_median', 'bytes_read_median'):
            fit = linear_fit([float(row['n_prime']) for row in members], [float(row[metric]) for row in members])
            output.append({'transition': transition, 'backend': backend, 'x': 'n_prime', 'y': metric, **fit})
    return output

def _migration_cost_catalog(summary_rows: Sequence[dict[str, Any]], *, preferred_backend: str='node_local') -> list[dict[str, Any]]:
    candidate_rows = [row for row in summary_rows if row['variant_label'] == 'canonical' and row['backend'] == preferred_backend]
    if not candidate_rows:
        candidate_rows = [row for row in summary_rows if row['variant_label'] == 'canonical']
    by_key: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        by_key[int(row['n']), str(row['transition'])].append(row)
    output = []
    for (n, transition), members in sorted(by_key.items()):
        output.append({'schema_version': 1, 'n': n, 'n_prime': int(members[0]['n_prime']), 'transition': transition, 'backend': preferred_backend if any((row['backend'] == preferred_backend for row in members)) else members[0]['backend'], 'wall_ns_median': _median((row['wall_ns_median'] for row in members)), 'cpu_ns_median': _median((row['cpu_ns_median'] for row in members)), 'bytes_read_median': _median((row['bytes_read_median'] for row in members)), 'bytes_written_median': _median((row['total_bytes_written_median'] for row in members)), 'target_serialized_bytes_median': _median((row['target_serialized_bytes_median'] for row in members)), 'temporary_disk_bytes_median': _median((row['temporary_disk_bytes_median'] for row in members)), 'source': 'Experiment-6-E6-measured-migration-costs'})
    return output

def _write_migration_figures(output: Path, summary_rows: Sequence[dict[str, Any]]) -> None:
    canonical = [row for row in summary_rows if row['variant_label'] == 'canonical' and row['backend'] == 'node_local']
    if not canonical:
        canonical = [row for row in summary_rows if row['variant_label'] == 'canonical']
    wall: dict[str, list[tuple[float, float]]] = defaultdict(list)
    hashes: dict[str, list[tuple[float, float]]] = defaultdict(list)
    temp: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in canonical:
        transition = str(row['transition'])
        wall[transition].append((float(row['n_prime']), float(row['wall_ns_median']) / 1000000.0))
        hashes[transition].append((float(row['n_prime']), float(row['hash_total_calls_median'])))
        temp[transition].append((float(row['n_prime']), float(row['temporary_disk_bytes_median'])))
    line_chart(output / 'fig_E6_migration_latency.svg', title='Root-preserving mode-migration latency', x_label="Padded epoch length n'", y_label='Median wall time (ms)', series=wall, x_log2=True)
    line_chart(output / 'fig_E6_hash_scaling.svg', title='Instrumented SHA-256 work during mode migration', x_label="Padded epoch length n'", y_label='SHA-256 invocations', series=hashes, x_log2=True, y_log2=True)
    line_chart(output / 'fig_E6_temporary_disk.svg', title='Temporary disk requirement during mode migration', x_label="Padded epoch length n'", y_label='Peak additional bytes', series=temp, x_log2=True, y_log2=True)

def analyze_migration(migration_plan: str | Path, raw_dir: str | Path, output_dir: str | Path, *, preferred_backend: str='node_local') -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    audit = inspect_raw(plan_path=migration_plan, raw_dir=raw_dir, kind='migration')
    if not audit['complete']:
        raise RuntimeError(f"migration results incomplete: missing={audit['missing_indices']} corrupt={audit['corrupt_cases']}")
    rows = [_migration_launch_row(result) for _, _, result in audit['results']]
    perf_summary = _group_migration_perf(rows)
    model_rows = _migration_models(perf_summary)
    cost_rows = _migration_cost_catalog(perf_summary, preferred_backend=preferred_backend)
    csv_write(output / 'migration_case_results.csv', rows)
    csv_write(output / 'migration_performance_summary.csv', perf_summary)
    csv_write(output / 'migration_models.csv', model_rows)
    csv_write(output / 'migration_cost_catalog.csv', cost_rows)
    hypothesis: list[dict[str, Any]] = []
    perf = [row for row in rows if row['trial_kind'] == 'migration_perf']
    exact_hash = True
    hash_observed = []
    exact_sizes = True
    size_observed = []
    for row in perf:
        source = str(row['source_mode'])
        n_prime = int(row['n_prime'])
        expected_leaf, expected_internal, expected_total = _expected_hashes(source, int(row['n']), n_prime)
        observed = (int(row['hash_leaf_calls']), int(row['hash_internal_calls']), int(row['hash_total_calls']))
        exact_hash = exact_hash and observed == (expected_leaf, expected_internal, expected_total)
        hash_observed.append({'case_id': row['case_id'], 'observed': observed, 'expected': (expected_leaf, expected_internal, expected_total)})
        target = str(row['target_mode'])
        if target in {'full', 'leaf'}:
            expected_size = support_size_formula(target, n_prime)
            exact_sizes = exact_sizes and int(row['target_serialized_bytes']) == expected_size
            size_observed.append({'case_id': row['case_id'], 'observed': int(row['target_serialized_bytes']), 'expected': expected_size})
    _check(hypothesis, 'H6a', 'instrumented hashing follows the exact linear work formula for all six transitions', bool(perf) and exact_hash, {'cases': len(perf), 'mismatches': sum((1 for row in hash_observed if tuple(row['observed']) != tuple(row['expected'])))}, "local-source transitions use 2(n'-1) internal hashes (with the n'=1 full-mode payload special case); external-source transitions hash n canonical blocks plus 3(n'-1) internal nodes")
    _check(hypothesis, 'H6a', 'Full and Leaf target serialization sizes follow the exact formulas', bool(size_observed) and exact_sizes, {'checked': len(size_observed), 'mismatches': sum((1 for row in size_observed if row['observed'] != row['expected']))}, "Full=64+(2n'-2)×32 bytes; Leaf=64+n'×32 bytes")
    ext_vs_local: dict[int, dict[str, float]] = defaultdict(dict)
    for row in perf_summary:
        if row['variant_label'] != 'canonical' or row['backend'] != preferred_backend:
            continue
        ext_vs_local[int(row['n'])][str(row['transition'])] = float(row['hash_total_calls_median'])
    ext_extra_checks = []
    for n, values in ext_vs_local.items():
        for target in ('full', 'leaf'):
            ext_key = f'ext->{target}'
            local_key = f"{('leaf' if target == 'full' else 'full')}->{target}"
            if ext_key in values and local_key in values:
                ext_extra_checks.append(values[ext_key] >= values[local_key])
    _check(hypothesis, 'H6a', 'external-to-local recovery performs no less cryptographic work than local conversion', all(ext_extra_checks) if ext_extra_checks else None, {'comparisons': len(ext_extra_checks), 'passed': sum(ext_extra_checks)}, 'all matched ext→local cases have hash work greater than or equal to the corresponding local conversion', primary=False)
    invariant_rows = [row for row in rows if row['trial_kind'] in {'migration_perf', 'equivalence_audit'}]
    invariants_ok = bool(invariant_rows) and all((row.get('root_equal') and row.get('protected_bytes_equal') and row.get('allowed_metadata_difference_only') for row in invariant_rows))
    _check(hypothesis, 'H6b', 'authenticated root and protected MSI metadata remain bit-identical', invariants_ok, {'checked': len(invariant_rows), 'failures': sum((1 for row in invariant_rows if not (row.get('root_equal') and row.get('protected_bytes_equal') and row.get('allowed_metadata_difference_only'))))}, 'every successful migration preserves root and all protected fields; only mode and AuxRef may change')
    audits = [row for row in rows if row['trial_kind'] == 'equivalence_audit']
    relation_ok = bool(audits) and all((row.get('accepted_relation_equivalent') for row in audits))
    _check(hypothesis, 'H6b', 'pre/post accepted-query relation is unchanged', relation_ok, {'audit_cases': len(audits), 'positions_checked': sum((int(row.get('pre_positions_checked', 0)) + int(row.get('post_positions_checked', 0)) for row in audits))}, 'every audited pre-state and post-state accepts the same canonical positions, exhaustively for n≤4096 and by boundary sampling above that threshold')
    faults = [row for row in rows if row['trial_kind'] == 'fault_injection']
    expected_fault_pairs = {(int(case['n']), str(case['transition']), str(case['fault'])) for case in jsonl_iter(migration_plan) if case['trial_kind'] == 'fault_injection'}
    observed_fault_pairs = {(int(row['n']), str(row['transition']), str(row['fault'])) for row in faults}
    fault_safety = bool(faults) and all((row.get('fault_injected') and row.get('active_state_valid') and (not row.get('mixed_state_detected', False)) for row in faults))
    _check(hypothesis, 'H6c', 'all planned crash and corruption cases expose a valid old or valid new state', fault_safety and expected_fault_pairs == observed_fault_pairs, {'planned': len(expected_fault_pairs), 'observed': len(observed_fault_pairs), 'mixed_states': sum((1 for row in faults if row.get('mixed_state_detected'))), 'invalid_active_states': sum((1 for row in faults if not row.get('active_state_valid')))}, 'complete fault matrix; zero mixed states and zero invalid active states')
    crash_rows = [row for row in faults if row['fault'] in CRASH_POINTS]
    reject_rows = [row for row in faults if row['fault'] not in CRASH_POINTS]
    _check(hypothesis, 'H6c', 'crash recovery and pre-commit rejection are both exercised', bool(crash_rows) and bool(reject_rows) and all((row.get('migration_rejected') for row in reject_rows)), {'crash_cases': len(crash_rows), 'rejection_cases': len(reject_rows)}, 'at least one crash-boundary case and one corruption/registration rejection case; all rejection cases fail before metadata commit', primary=False)
    csv_write(output / 'hypothesis_checks_E6.csv', hypothesis)
    _write_migration_figures(output, perf_summary)
    primary = [row for row in hypothesis if row['primary'] and row['status'] != 'NOT_EVALUATED']
    summary = {'schema_version': 1, 'experiment': 'E6', 'status': 'PASS' if audit['complete'] else 'FAIL', 'plan_audit': {key: value for key, value in audit.items() if key != 'results'}, 'migration_performance_cases': len(perf), 'equivalence_audit_cases': len(audits), 'fault_injection_cases': len(faults), 'migration_cost_rows': len(cost_rows), 'all_primary_hypotheses_pass': bool(primary) and all((row['status'] == 'PASS' for row in primary)), 'hypothesis_status': {name: 'PASS' if all((row['status'] == 'PASS' for row in hypothesis if row['hypothesis'] == name and row['primary'] and (row['status'] != 'NOT_EVALUATED'))) else 'FAIL' for name in ('H6a', 'H6b', 'H6c')}}
    atomic_write_json(output / 'summary_E6.json', summary)
    return summary

def _policy_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for _, case, result in audit['results']:
        for row in result['results']:
            flat = {'case_id': result['case_id'], 'scale_label': case['scale_label'], 'epoch_count': case['epoch_count'], 'shard_count': case['shard_count'], 'n_values': json.dumps(case['n_values'], separators=(',', ':')), 'profile': case['profile'], 'alpha': case['profile_weights']['alpha'], 'beta': case['profile_weights']['beta'], 'gamma': case['profile_weights']['gamma'], 'delta': case['profile_weights']['delta'], 'budget_fraction': case['budget_fraction'], 'workload': case['workload'], 'prediction_error': case['prediction_error'], 'trace_replicates': case['trace_replicates'], 'evaluation_queries': case['evaluation_queries'], 'run_dp': case['run_dp'], 'component_catalog_publication_eligible': result['component_catalog_publication_eligible'], 'component_catalog_sha256': result.get('component_catalog_sha256'), 'migration_catalog_sha256': result.get('migration_catalog_sha256')}
            for key, value in row.items():
                if key in {'solver_metadata', 'normalisation'}:
                    flat[key] = json.dumps(value, sort_keys=True, separators=(',', ':'))
                else:
                    flat[key] = value
            output.append(flat)
    return output

def _group_policy(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ('scale_label', 'profile', 'budget_fraction', 'workload', 'prediction_error', 'algorithm')
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple((row[key] for key in keys))].append(row)
    metrics = ('storage_bytes', 'planned_steady_objective', 'realised_steady_objective', 'planned_amortised_objective', 'realised_amortised_objective', 'planned_optimality_gap', 'realised_oracle_gap', 'expected_response_bytes_per_query', 'expected_cpu_ns_per_query', 'expected_energy_joules_per_query', 'completion_probability', 'migration_count', 'migration_wall_ns', 'migration_bytes_read', 'migration_bytes_written', 'migration_penalty', 'break_even_queries', 'mode_full_count', 'mode_leaf_count', 'mode_ext_count')
    output = []
    for key, members in sorted(groups.items(), key=lambda pair: tuple(map(str, pair[0]))):
        row = dict(zip(keys, key))
        row['observations'] = len(members)
        row['feasible_fraction'] = sum((bool(member['feasible']) for member in members)) / len(members)
        for metric in metrics:
            values = [float(member[metric]) for member in members if member.get(metric) not in (None, '')]
            stats = summarise(values)
            row[f'{metric}_median'] = stats['median']
            row[f'{metric}_p95'] = stats['p95']
        output.append(row)
    return output

def _write_policy_figures(output: Path, grouped: Sequence[dict[str, Any]]) -> None:
    focus = [row for row in grouped if row['scale_label'] == 'primary' and row['profile'] == 'balanced' and (row['workload'] == 'zipf1.2') and (float(row['prediction_error']) == 0.25)]
    objective: dict[str, list[tuple[float, float]]] = defaultdict(list)
    modes: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in focus:
        if row['realised_amortised_objective_median'] is not None:
            objective[str(row['algorithm'])].append((100.0 * float(row['budget_fraction']), float(row['realised_amortised_objective_median'])))
        if row['algorithm'] == 'dynamic_programming':
            modes['Full'].append((100.0 * float(row['budget_fraction']), float(row['mode_full_count_median'])))
            modes['Leaf'].append((100.0 * float(row['budget_fraction']), float(row['mode_leaf_count_median'])))
            modes['Ext'].append((100.0 * float(row['budget_fraction']), float(row['mode_ext_count_median'])))
    line_chart(output / 'fig_E9_objective_vs_budget.svg', title='Realised migration-amortised objective under storage budgets', x_label='Storage budget (% of all-Full)', y_label='Median realised objective', series=objective)
    line_chart(output / 'fig_E9_mode_composition.svg', title='Dynamic-programming mode composition', x_label='Storage budget (% of all-Full)', y_label='Median number of epochs', series=modes)
    error_focus = [row for row in grouped if row['scale_label'] == 'primary' and row['profile'] == 'balanced' and (float(row['budget_fraction']) == 0.5) and (row['algorithm'] == 'dynamic_programming')]
    regret: dict[str, list[tuple[float, float]]] = defaultdict(list)
    migration: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in error_focus:
        regret[str(row['workload'])].append((100.0 * float(row['prediction_error']), float(row['realised_oracle_gap_median'] or 0.0)))
        migration[str(row['workload'])].append((100.0 * float(row['prediction_error']), float(row['migration_count_median'] or 0.0)))
    line_chart(output / 'fig_E9_prediction_error.svg', title='Allocation regret under workload-prediction error', x_label='Prediction error (%)', y_label='Median realised gap to actual-workload oracle', series=regret)
    line_chart(output / 'fig_E9_migration_churn.svg', title='Migration churn under workload-prediction error', x_label='Prediction error (%)', y_label='Median migrations per reallocation window', series=migration)

def analyze_all(*, config_path: str | Path, migration_plan: str | Path, migration_raw: str | Path, policy_plan: str | Path, policy_raw: str | Path, component_costs: str | Path, output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    config = read_json(config_path)
    e6 = analyze_migration(migration_plan, migration_raw, output, preferred_backend=str(config['migration'].get('policy_cost_backend', 'node_local')))
    policy_audit = inspect_raw(plan_path=policy_plan, raw_dir=policy_raw, kind='policy')
    if not policy_audit['complete']:
        raise RuntimeError(f"policy results incomplete: missing={policy_audit['missing_indices']} corrupt={policy_audit['corrupt_cases']}")
    catalog = load_cost_catalog(component_costs)
    catalog_audit = validate_cost_catalog(catalog, required_n=config['policy']['n_values'], require_publication_eligible=bool(config.get('publication_mode', False)))
    if catalog_audit['status'] != 'PASS':
        raise RuntimeError(f"component cost catalog failed validation: {catalog_audit['failures']}")
    expected_component_digest = sha256_file(component_costs)
    expected_migration_digest = sha256_file(output / 'migration_cost_catalog.csv')
    catalog_digest_failures = []
    for _index, _case, raw_result in policy_audit['results']:
        if raw_result.get('component_catalog_sha256') != expected_component_digest:
            catalog_digest_failures.append(f"{raw_result.get('case_id')}: component catalog digest mismatch")
        if raw_result.get('migration_catalog_sha256') != expected_migration_digest:
            catalog_digest_failures.append(f"{raw_result.get('case_id')}: migration catalog digest mismatch")
    if catalog_digest_failures:
        raise RuntimeError('policy input-catalog mismatch: ' + '; '.join(catalog_digest_failures[:10]))
    policy_rows = _policy_rows(policy_audit)
    grouped = _group_policy(policy_rows)
    csv_write(output / 'policy_observations.csv', policy_rows)
    csv_write(output / 'policy_summary.csv', grouped)
    hypothesis: list[dict[str, Any]] = []
    dp_rows = [row for row in policy_rows if row['algorithm'] == 'dynamic_programming']
    dp_optimal = bool(dp_rows) and all((row['feasible'] and abs(float(row['planned_optimality_gap'])) <= 1e-10 for row in dp_rows))
    _check(hypothesis, 'H9a', 'exact dynamic programming attains the minimum migration-amortised planned objective', dp_optimal, {'observations': len(dp_rows), 'maximum_gap': max((abs(float(row['planned_optimality_gap'])) for row in dp_rows), default=None)}, 'every DP observation is feasible and has planned optimality gap ≤ 1e-10 relative to all evaluated algorithms; the solver is exact for 4 KiB-aligned costs')
    adaptive = [row for row in policy_rows if row['algorithm'] in {'dynamic_programming', 'greedy_ratio', 'age_tier', 'popularity_tier'}]
    budget_ok = bool(adaptive) and all((row['feasible'] and int(row['budget_violation_bytes']) == 0 for row in adaptive))
    fixed_infeasible_reported = all((bool(row['feasible']) and int(row['budget_violation_bytes']) == 0 or (not bool(row['feasible']) and int(row['budget_violation_bytes']) > 0) for row in policy_rows if row['algorithm'] in {'all_full', 'all_leaf', 'all_ext'}))
    _check(hypothesis, 'H9a', 'adaptive policies satisfy the storage constraint and infeasible fixed baselines are explicitly reported', budget_ok and fixed_infeasible_reported, {'adaptive_rows': len(adaptive), 'adaptive_violations': sum((1 for row in adaptive if not row['feasible'])), 'fixed_infeasible_rows': sum((1 for row in policy_rows if row['algorithm'] in {'all_full', 'all_leaf', 'all_ext'} and (not row['feasible'])))}, 'zero silent budget violations')
    profile_cells: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in grouped:
        if row['algorithm'] != 'dynamic_programming':
            continue
        key = (row['scale_label'], row['budget_fraction'], row['workload'], row['prediction_error'])
        profile_cells[key][str(row['profile'])] = row
    sensitivity_count = 0
    storage_wins = 0
    bandwidth_wins = 0
    cpu_wins = 0
    availability_wins = 0
    for cell in profile_cells.values():
        if not all((name in cell for name in ('balanced', 'storage', 'bandwidth', 'cpu', 'availability'))):
            continue
        sensitivity_count += 1
        storage_wins += float(cell['storage']['storage_bytes_median']) <= float(cell['balanced']['storage_bytes_median']) + 1e-09
        bandwidth_wins += float(cell['bandwidth']['expected_response_bytes_per_query_median']) <= float(cell['balanced']['expected_response_bytes_per_query_median']) + 1e-09
        cpu_wins += float(cell['cpu']['expected_cpu_ns_per_query_median']) <= float(cell['balanced']['expected_cpu_ns_per_query_median']) + 1e-09
        availability_wins += float(cell['availability']['completion_probability_median']) + 1e-12 >= float(cell['balanced']['completion_probability_median'])
    sensitivity_ok = min(storage_wins, bandwidth_wins, cpu_wins, availability_wins) / sensitivity_count >= 0.5 if sensitivity_count > 0 else None
    _check(hypothesis, 'H9b', 'changing resource weights changes the selected allocation in the intended direction', sensitivity_ok, {'matched_cells': sensitivity_count, 'storage_cells': storage_wins, 'bandwidth_cells': bandwidth_wins, 'cpu_cells': cpu_wins, 'availability_cells': availability_wins}, 'in a majority of matched cells, emphasizing a resource does not worsen that resource metric relative to the balanced profile')
    charged = [row for row in policy_rows if int(row['migration_count']) > 0]
    migration_accounted = bool(charged) and all((float(row['migration_penalty']) > 0 and float(row['migration_wall_ns']) > 0 and (float(row['migration_bytes_written']) > 0) for row in charged))
    _check(hypothesis, 'H9c', 'measured E6 migration time and bytes are charged to the first evaluation window', migration_accounted, {'charged_rows': len(charged), 'unaccounted': sum((1 for row in charged if not (float(row['migration_penalty']) > 0 and float(row['migration_wall_ns']) > 0 and (float(row['migration_bytes_written']) > 0))))}, 'every changed assignment has positive measured migration time, bytes and amortised penalty')
    error_groups: dict[tuple[str, str, float, str], dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in dp_rows:
        key = (str(row['scale_label']), str(row['profile']), float(row['budget_fraction']), str(row['workload']))
        error_groups[key][float(row['prediction_error'])].append(float(row['realised_oracle_gap']))
    zero_gaps = []
    higher_gaps = []
    for values in error_groups.values():
        if 0.0 in values:
            zero_gaps.extend(values[0.0])
            for error, samples in values.items():
                if error > 0:
                    higher_gaps.extend(samples)
    prediction_ok = bool(zero_gaps) and max((abs(value) for value in zero_gaps)) <= 1e-10 and bool(higher_gaps) and (_median(higher_gaps) >= _median(zero_gaps) - 1e-12)
    _check(hypothesis, 'H9c', 'perfect predictions reproduce the actual-workload oracle and imperfect predictions expose allocation regret', prediction_ok, {'perfect_prediction_max_gap': max((abs(value) for value in zero_gaps), default=None), 'higher_error_median_gap': _median(higher_gaps) if higher_gaps else None}, 'error=0 DP gap ≤ 1e-10 and median gap at error>0 is not lower than the perfect-prediction median')
    csv_write(output / 'hypothesis_checks_E9.csv', hypothesis)
    combined_hypothesis = []
    for filename in ('hypothesis_checks_E6.csv', 'hypothesis_checks_E9.csv'):
        with (output / filename).open(encoding='utf-8', newline='') as handle:
            combined_hypothesis.extend(csv.DictReader(handle))
    csv_write(output / 'hypothesis_checks.csv', combined_hypothesis)
    _write_policy_figures(output, grouped)
    primary_e9 = [row for row in hypothesis if row['primary'] and row['status'] != 'NOT_EVALUATED']
    e9_status = bool(primary_e9) and all((row['status'] == 'PASS' for row in primary_e9))
    integrity_status = e6['status'] == 'PASS' and policy_audit['complete'] and (catalog_audit['status'] == 'PASS')
    all_hypotheses = bool(e6['all_primary_hypotheses_pass'] and e9_status)
    summary = {'schema_version': 1, 'experiment': 'E6+E9', 'status': 'PASS' if integrity_status else 'FAIL', 'migration': e6, 'policy_plan_audit': {key: value for key, value in policy_audit.items() if key != 'results'}, 'component_catalog_audit': catalog_audit, 'policy_observations': len(policy_rows), 'policy_summary_rows': len(grouped), 'all_primary_hypotheses_pass': all_hypotheses, 'hypothesis_status': {**e6['hypothesis_status'], **{name: 'NOT_EVALUATED' if not [row for row in hypothesis if row['hypothesis'] == name and row['primary'] and (row['status'] != 'NOT_EVALUATED')] else 'PASS' if all((row['status'] == 'PASS' for row in hypothesis if row['hypothesis'] == name and row['primary'] and (row['status'] != 'NOT_EVALUATED'))) else 'FAIL' for name in ('H9a', 'H9b', 'H9c')}}, 'publication_mode': bool(config.get('publication_mode', False)), 'component_catalog_publication_eligible': catalog_audit['all_publication_eligible'], 'component_catalog_sha256': expected_component_digest, 'migration_catalog_sha256': expected_migration_digest, 'policy_catalog_digests_consistent': True}
    atomic_write_json(output / 'summary.json', summary)
    report = ['# Experiment 6: Root-Preserving Refolding and Resource-Aware Mode Allocation', '', f"Overall status: **{summary['status']}**", '', '## Completeness', '', f"- E6 migration cases: {e6['plan_audit']['valid_cases']} / {e6['plan_audit']['expected_cases']}", f"- E9 policy cases: {policy_audit['valid_cases']} / {policy_audit['expected_cases']}", f'- Policy observation rows: {len(policy_rows)}', f"- Component catalog publication eligible: {catalog_audit['all_publication_eligible']}", '', '## Hypothesis status', '']
    for name, status in summary['hypothesis_status'].items():
        report.append(f'- {name}: **{status}**')
    report.extend(['', '## Interpretation boundary', '', 'E6 measures state migration, atomicity, recovery, and representation equivalence. E9 consumes E1/E2/E3/availability component costs plus the E6 migration-cost catalog; it does not remeasure those components.', '', 'The fixed all-Full, all-Leaf, and all-Ext rows remain in the output even when infeasible. Their budget violations are explicit and are never treated as feasible optimization results.'])
    atomic_write_text(output / 'report.md', '\n'.join(report) + '\n')
    _write_manifest(output)
    return summary

def validate_results(results_dir: str | Path) -> dict[str, Any]:
    root = Path(results_dir)
    required = ['summary.json', 'report.md', 'migration_case_results.csv', 'migration_performance_summary.csv', 'migration_cost_catalog.csv', 'policy_observations.csv', 'policy_summary.csv', 'hypothesis_checks.csv', 'manifest.sha256']
    missing = [name for name in required if not (root / name).exists()]
    manifest = verify_manifest(root) if not missing else {'status': 'FAIL', 'checked': 0, 'failures': []}
    summary = read_json(root / 'summary.json') if (root / 'summary.json').exists() else {}
    failures = list(missing)
    failures.extend(manifest.get('failures', []))
    if summary.get('status') != 'PASS':
        failures.append(f"summary status is {summary.get('status')}")
    return {'status': 'PASS' if not failures else 'FAIL', 'missing': missing, 'manifest': manifest, 'summary_status': summary.get('status'), 'failures': failures}
