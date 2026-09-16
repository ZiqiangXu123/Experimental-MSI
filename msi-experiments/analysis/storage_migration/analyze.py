#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
RUNS = {'Pi_Ethernet2': 'ethernet2/pi_a_paper_ethereum_ethernet2', 'Pi_WiFi': 'Pi_results/pi-a-paper_ethereum_wifi', 'HPC': 'HPC_results/hpc_real_422175'}
MODES = ['archive', 'full', 'leaf', 'ext_cached', 'ext_rebuild']
TRANSITIONS = ['full->leaf', 'leaf->full', 'full->ext', 'leaf->ext', 'ext->leaf', 'ext->full']

def read_json(path):
    return json.loads(path.read_text())

def read_rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

def stats(values):
    valid = [x for x in values if isinstance(x, (int, float)) and (not isinstance(x, bool))]
    return dict(median=median(valid) if valid else None, min=min(valid) if valid else None, max=max(valid) if valid else None, count=len(valid), missing=len(values) - len(valid))

def write_csv(path, rows):
    keys = list(dict.fromkeys((k for r in rows for k in r)))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, keys)
        writer.writeheader()
        writer.writerows(rows)

def add_stats(target, prefix, values):
    target.update({prefix + '_' + k: v for k, v in stats(values).items()})

def same_number(a, b):
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-09)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-root', type=Path, default=Path('results_intake'))
    ap.add_argument('--out', type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args()
    root, out = (args.input_root.resolve(), args.out.resolve())
    if not root.is_dir():
        ap.error('Input root not found; provide --input-root containing the extracted result archives.')
    if out == root or root in out.parents:
        ap.error('Derived output must be outside the read-only input tree.')
    out.mkdir(exist_ok=True, parents=True)
    storage, provider, migration, recovery, robustness, verification, hashes = ([], [], [], [], [], {}, {})
    datasets, e6_sources = ({}, {})
    for run, rel in RUNS.items():
        base = root / rel
        rows = read_rows(base / 'results.jsonl')
        manifest = read_json(base / 'run_manifest.json')
        check = {'completed': bool(manifest.get('completed_unix_ns')), 'failed_records': manifest.get('failed_records'), 'counts': dict(Counter((r['experiment'] + ':' + r['status'] for r in rows))), 'storage_errors': [], 'provider_raw_errors': [], 'migration_raw_errors': [], 'provider_raw_rows': 0, 'provider_raw_samples': 0, 'migration_checks': Counter(), 'migration_snapshots': [], 'memory_errors': []}
        for p in [base / 'results.jsonl', base / 'run_manifest.json']:
            hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        datasets[run] = sorted({(r['data_kind'], r['provenance']['n'], r['provenance']['selected_payload_sha256']) for r in rows if r['experiment'] == 'storage'})
        grouped = defaultdict(list)
        for r in rows:
            if r['experiment'] != 'storage':
                continue
            m, p = (r['metrics'], r['provenance'])
            grouped[r['data_kind'], p['n'], p['mode']].append(r)
            n, npad = (m['n'], m['n_prime'])
            expected_support = 0 if m['mode'] == 'ext_rebuild' else 32 * (npad if m['mode'] == 'leaf' else 2 * npad - 1)
            assertions = {'role_total': m['operational_total_bytes'] == m['verifier_total_bytes'] + m['provider_total_bytes'], 'materialized_total': m['total_materialized_bytes'] == m['operational_total_bytes'] + m['setup_total_bytes'], 'setup_seed': m['setup_total_bytes'] == m['setup_signing_key_bytes'] == 32, 'payload_and_envelope': m['verifier_payload_bytes'] + m['provider_payload_bytes'] == m['raw_opaque_payload_bytes'] + 36 * n, 'envelope_36n': m['canonical_envelope_overhead_bytes'] == 36 * n, 'support_formula': m['provider_support_bytes'] + m['verifier_support_bytes'] == expected_support}
            if m['mode'] != 'archive':
                assertions['descriptor_only'] = m['verifier_total_bytes'] == m['descriptor_serialized_bytes']
                assertions['verifier_no_payload_support'] = m['verifier_payload_bytes'] == m['verifier_support_bytes'] == 0
            if 'physical_deployed_case_total_bytes' in m:
                assertions['remote_materialized_total'] = m['physical_deployed_case_total_bytes'] == m['total_materialized_bytes'] + m['descriptor_serialized_bytes']
            for k, ok in assertions.items():
                if not ok:
                    check['storage_errors'].append([r['case_id'], k])
        for (kind, n, mode), rr in sorted(grouped.items()):
            x = dict(run=run, data_kind=kind, n=n, mode=mode, trials=len(rr))
            assert len(rr) == 5 and {r['provenance']['trial'] for r in rr} == set(range(5))
            keys = sorted({k for r in rr for k, v in r['metrics'].items() if k.endswith('_bytes') and isinstance(v, (int, float))})
            for k in keys:
                add_stats(x, k, [r['metrics'].get(k) for r in rr])
            storage.append(x)
        grouped = defaultdict(list)
        for r in rows:
            if r['experiment'] != 'provider' or r['status'] != 'ok':
                continue
            p = r['provenance']
            grouped[r['data_kind'], p['n'], p['mode'], p['source']].append(r)
            if p['source'] != 'warm_network_queries':
                continue
            rawpath = base / 'raw' / (r['case_id'].removesuffix('-provider') + '.json')
            raw = read_json(rawpath)
            hashes[str(rawpath.relative_to(root))] = hashlib.sha256(rawpath.read_bytes()).hexdigest()
            ss = raw['provider_samples']
            expected = {'queries': len(ss), 'median_ms': median((s['provider_total_ns'] / 1000000.0 for s in ss)), 'provider_cpu_ms_per_query': sum((s['provider_cpu_ns'] for s in ss)) / len(ss) / 1000000.0, 'payload_read_bytes_per_query': sum((s['payload_read_bytes'] for s in ss)) / len(ss), 'support_read_bytes_per_query': sum((s['support_read_bytes'] for s in ss)) / len(ss), 'witness_generate_median_ms': median((s['witness_generate_ns'] / 1000000.0 for s in ss)), 'rss_bytes': max((s['rss_bytes'] for s in ss))}
            for k, value in expected.items():
                if not same_number(r['metrics'].get(k), value):
                    check['provider_raw_errors'].append([r['case_id'], k, value, r['metrics'].get(k)])
            for i, s in enumerate(ss):
                wanted = p['n'] if p['mode'] == 'ext_rebuild' else 1
                if s['payload_read_operations'] != wanted or s['rebuilds_tree_each_query'] != (p['mode'] == 'ext_rebuild'):
                    check['provider_raw_errors'].append([r['case_id'], i, 'payload/rebuild accounting'])
            check['provider_raw_rows'] += 1
            check['provider_raw_samples'] += len(ss)
        for (kind, n, mode, source), rr in sorted(grouped.items()):
            assert len(rr) == 5 and {r['provenance']['trial'] for r in rr} == set(range(5))
            x = dict(run=run, data_kind=kind, n=n, mode=mode, source=source, trials=len(rr))
            for k in sorted({k for r in rr for k, v in r['metrics'].items() if isinstance(v, (int, float)) or v is None}):
                add_stats(x, k, [r['metrics'].get(k) for r in rr])
            provider.append(x)
        mr = [r for r in rows if r['experiment'] == 'migration']
        check['migration_cases'] = len(mr)
        check['migration_faults'] = dict(Counter((r['metrics']['fault'] for r in mr)))
        e6_sources[run] = [r['provenance']['source_sha256'] for r in mr]
        raw_paths = {p.name: p for p in base.glob('migration/*/raw-e6/*.json')}
        grouped = defaultdict(list)
        for r in mr:
            m = r['metrics']
            rawpath = raw_paths.get(r['case_id'] + '.json')
            if rawpath is None:
                check['migration_raw_errors'].append([r['case_id'], 'missing raw'])
                continue
            raw = read_json(rawpath)
            hashes[str(rawpath.relative_to(root))] = hashlib.sha256(rawpath.read_bytes()).hexdigest()
            if raw.get('all_checks_pass') is not True or r['status'] != 'ok':
                check['migration_raw_errors'].append([r['case_id'], 'all checks pass/status'])
            for k in ['checks', 'root_hex_before', 'root_hex_after', 'protected_sha256_before', 'protected_sha256_after', 'pre_query_audit', 'post_query_audit', 'recovery', 'child_exit_code', 'migration_wall_ns', 'migration_cpu_ns']:
                if k in m and k in raw and (m[k] != raw[k]):
                    check['migration_raw_errors'].append([r['case_id'], k])
            for k, ok in m['checks'].items():
                check['migration_checks'][k + ':' + str(ok)] += 1
                if ok is not True:
                    check['migration_raw_errors'].append([r['case_id'], k, ok])
            if not (m['root_preserved'] and m['queries_equivalent'] and (m['root_hex_before'] == m['root_hex_after']) and (m['protected_sha256_before'] == m['protected_sha256_after'])):
                check['migration_raw_errors'].append([r['case_id'], 'protected/root/equivalence'])
            for phase in ['snapshot_before', 'snapshot_after']:
                snap = m.get(phase, {})
                check['migration_snapshots'].append({'case_id': r['case_id'], 'phase': phase, 'temp_C': snap.get('temp_C'), 'throttled': snap.get('throttled')})
            if m['fault'] == 'none':
                grouped[m['transition']].append(r)
            else:
                rec = m['recovery']
                recovery.append({'run': run, 'transition': m['transition'], 'fault': m['fault'], 'replicates_at_this_fault': 1, 'recovery_ms': rec['recovery_ns'] / 1000000.0, 'status': rec['recovery_status'], 'case_wall_ms': m['case_wall_ns'] / 1000000.0, 'expected_active_mode': m['expected_active_mode'], 'actual_active_mode': m['active_mode'], 'child_exit_code': m['child_exit_code'], 'journals_found': rec.get('journals_found'), 'temporary_metadata_files': rec.get('temporary_metadata_files'), 'unreferenced_target_objects_cleaned': rec.get('unreferenced_target_objects_cleaned'), 'pre_audit_positions': m['pre_query_audit']['positions_checked'], 'post_audit_positions': m['post_query_audit']['positions_checked']})
        for direction, rr in sorted(grouped.items()):
            assert len(rr) == 5 and {r['metrics']['repeat'] for r in rr} == set(range(5))
            x = dict(run=run, transition=direction, clean_repeats=len(rr), n=512, opaque_block_bytes=2048)
            for k in ['migration_wall_ns', 'migration_cpu_ns', 'case_wall_ns', 'temporary_disk_bytes', 'target_serialized_bytes', 'metadata_bytes_written', 'write_amplification']:
                kk = k.removesuffix('_ns') + '_ms' if k.endswith('_ns') else k
                add_stats(x, kk, [r['metrics'].get(k) / 1000000.0 if k.endswith('_ns') and r['metrics'].get(k) is not None else r['metrics'].get(k) for r in rr])
            for k in ['bytes_read', 'bytes_written']:
                add_stats(x, 'logical_io_' + k, [r['metrics']['io'][k] for r in rr])
            for k in ['leaf', 'internal', 'total']:
                add_stats(x, 'hash_calls_' + k, [r['metrics']['hash_calls'][k] for r in rr])
            x['filesystem'] = ','.join(sorted({r['platform']['filesystem']['filesystem_type'] for r in rr}))
            x['fixture_teardown_policy'] = ','.join(sorted({r['provenance'].get('workspace_cleanup_policy', 'prepatch cleanup') for r in rr}))
            migration.append(x)
        pressure = [r for r in rows if r['experiment'] == 'memory_pressure']
        for r in pressure:
            m, p = (r['metrics'], r['provenance'])
            if not (r['status'] == 'ok' and m['queries'] == m['accepted'] == 200 and (m['memory_limit_mib'] == 256) and (m['pressure_buffer_bytes'] == 64 * 1048576)):
                check['memory_errors'].append(r['case_id'])
        integrity = [r for r in rows if r['experiment'] == 'integrity']
        disconnect = [r for r in rows if r['experiment'] == 'disconnect']
        robustness.append({'run': run, 'memory_cases': len(pressure), 'memory_accepted': sum((r['metrics']['accepted'] for r in pressure)), 'memory_queries': sum((r['metrics']['queries'] for r in pressure)), 'RLIMIT_AS_MiB': 256, 'touched_pressure_buffer_MiB': 64, 'integrity_cases': sum((r['metrics'].get('cases', 0) for r in integrity)), 'integrity_failures': sum((r['metrics'].get('failures', 0) for r in integrity)), 'TCP_disconnect_cases': len(disconnect), 'TCP_disconnect_recovered': sum((bool(r['metrics'].get('recovered')) for r in disconnect)), 'TCP_disconnect_false_accepts': sum((r['metrics'].get('false_accepts', 0) for r in disconnect)), 'migration_cases': len(mr), 'migration_clean': sum((r['metrics']['fault'] == 'none' for r in mr)), 'migration_process_crash': sum((r['metrics']['fault'] != 'none' for r in mr)), 'migration_pre_audit_positions': sum((r['metrics']['pre_query_audit']['positions_checked'] for r in mr)), 'migration_post_audit_positions': sum((r['metrics']['post_query_audit']['positions_checked'] for r in mr))})
        verification[run] = check
    verification['cross_run'] = {'recorded_selected_dataset_identities_equal': len({json.dumps(v) for v in datasets.values()}) == 1, 'all_original_E6_source_hashes_equal': len({json.dumps(v, sort_keys=True) for vv in e6_sources.values() for v in vv}) == 1, 'independent_original_dataset_rehashes': 0, 'dataset_limit': 'Original payload files omitted; compares recorded identities only.'}
    write_csv(out / 'storage_summary.csv', storage)
    write_csv(out / 'provider_summary.csv', provider)
    write_csv(out / 'migration_clean_summary.csv', migration)
    write_csv(out / 'recovery_by_fault.csv', recovery)
    write_csv(out / 'robustness_counts.csv', robustness)
    table = []
    for n in [512, 4096, 69]:
        for mode in MODES:
            s = next((x for x in storage if x['run'] == 'Pi_Ethernet2' and x['n'] == n and (x['mode'] == mode)))
            row = {'n': n, 'data_kind': s['data_kind'], 'mode': mode, 'verifier_total_KiB': s['verifier_total_bytes_median'] / 1024, 'verifier_support_KiB': s['verifier_support_bytes_median'] / 1024, 'provider_payload_KiB': s['provider_payload_bytes_median'] / 1024, 'provider_support_KiB': s['provider_support_bytes_median'] / 1024, 'provider_total_KiB': s['provider_total_bytes_median'] / 1024, 'operational_role_total_KiB': s['operational_total_bytes_median'] / 1024}
            for run in ['Pi_Ethernet2', 'Pi_WiFi']:
                p = next((x for x in provider if x['run'] == run and x['n'] == n and (x['mode'] == mode) and (x['source'] == 'warm_network_queries')), None)
                for metric in ['provider_cpu_ms_per_query', 'median_ms', 'payload_read_bytes_per_query', 'support_read_bytes_per_query', 'rss_bytes']:
                    for agg in ['median', 'min', 'max']:
                        row[run + '_' + metric + '_' + agg] = p.get(metric + '_' + agg) if p else None
            row['provider_cpu_scope'] = 'not applicable: archive is local' if mode == 'archive' else 'median of five trial mean thread CPU times'
            table.append(row)
    write_csv(out / 'main_storage_provider_candidate.csv', table)
    table = []
    for direction in TRANSITIONS:
        row = {'transition': direction, 'clean_repeats_per_platform': 5}
        for run in RUNS:
            r = next((x for x in migration if x['run'] == run and x['transition'] == direction))
            for metric in ['migration_wall_ms', 'migration_cpu_ms', 'logical_io_bytes_read', 'logical_io_bytes_written', 'temporary_disk_bytes']:
                for agg in ['median', 'min', 'max']:
                    row[run + '_' + metric + '_' + agg] = r[metric + '_' + agg]
        table.append(row)
    write_csv(out / 'main_migration_candidate.csv', table)
    (out / 'verification.json').write_text(json.dumps(verification, indent=2) + '\n')
    (out / 'input_hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')
    brief = {run: {k: len(v[k]) for k in ['storage_errors', 'provider_raw_errors', 'migration_raw_errors', 'memory_errors']} for run, v in verification.items() if run in RUNS}
    print(json.dumps({'checks': brief, 'cross_run': verification['cross_run']}, indent=2))
if __name__ == '__main__':
    main()
