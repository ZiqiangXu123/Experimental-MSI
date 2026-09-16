#!/usr/bin/env python3
from __future__ import annotations
import collections
import base64
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input-root', type=Path, default=Path('results_intake'))
parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent)
args = parser.parse_args()
INPUT = args.input_root.resolve()
OUT = args.out.resolve()
if not INPUT.is_dir():
    parser.error('Input root not found; provide the directory containing extracted result archives.')
if OUT == INPUT or INPUT in OUT.parents:
    parser.error('Derived output must be outside the read-only input tree.')
OUT.mkdir(parents=True, exist_ok=True)

def load(path):
    return json.loads(path.read_text())

def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def safe_ratio(x, y):
    return x / y if y else None

def groups(records, fn):
    result = collections.defaultdict(list)
    for r in records:
        result[fn(r)].append(r)
    return result

def counter(values):
    return dict(sorted(collections.Counter(values).items(), key=lambda kv: str(kv[0])))

def field_signature(record):
    p = record.get('provenance', {})
    return (record.get('data_kind'), record['experiment'], p.get('n'), p.get('mode', p.get('scheme')), p.get('trial'), p.get('source'))

def equal_number(x, y):
    return isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isclose(x, y, rel_tol=1e-11, abs_tol=1e-08)

def percentile(values, fraction):
    values = sorted(values)
    at = (len(values) - 1) * fraction
    lo, hi = (math.floor(at), math.ceil(at))
    return values[lo] + (values[hi] - values[lo]) * (at - lo)

def thermal_summary(telemetry):
    temps = [t['temp_C'] for t in telemetry if isinstance(t.get('temp_C'), (int, float))]
    flags = [t.get('throttled') for t in telemetry]
    known = []
    for s in flags:
        try:
            known.append(int(s.split('=')[-1], 16))
        except (ValueError, AttributeError):
            pass
    return {'snapshots': len(telemetry), 'temperature_min_C': min(temps) if temps else None, 'temperature_max_C': max(temps) if temps else None, 'flags': counter((str(f) for f in flags)), 'nonzero_current_low_four_bits': sum((bool(v & 15) for v in known)), 'known_flag_snapshots': len(known)}
all_audits = {}
all_records = {}
raw_by_run = {}
errors = []
for mp in sorted(INPUT.rglob('run_manifest.json')):
    base, name = (mp.parent, mp.parent.name)
    manifest = load(mp)
    records = rows(base / 'results.jsonl')
    all_records[name] = records
    raw_entries = {}
    input_cfg = {}
    thermal = collections.defaultdict(list)
    checks = []
    original_system = []
    rss_snapshots = []
    for rp in sorted((base / 'raw').glob('*.json')):
        d = load(rp)
        if rp.name.endswith('.input.json'):
            input_cfg[rp.name[:-len('.input.json')]] = d
            continue
        raw_entries[rp.stem] = d
        if 'error' in d:
            errors.append({'run': name, 'file': str(rp.relative_to(INPUT)), 'error': d})
        if 'raw_samples' in d:
            exp = rp.stem.rsplit('-', 1)[-1]
            samples = d['raw_samples']
            m = d['metrics']
            expected = {'queries': len(samples), 'median_ms': statistics.median((s['elapsed_ns'] for s in samples)) / 1000000.0, 'p95_ms': percentile([s['elapsed_ns'] for s in samples], 0.95) / 1000000.0, 'p99_ms': percentile([s['elapsed_ns'] for s in samples], 0.99) / 1000000.0, 'accept_median_ms': statistics.median((s['accept_ns'] for s in samples)) / 1000000.0, 'cpu_ms_per_query': statistics.mean((s['accept_cpu_ns'] for s in samples)) / 1000000.0}
            for metric, value in expected.items():
                if not equal_number(m.get(metric), value):
                    checks.append({'file': rp.name, 'metric': metric, 'stored': m.get(metric), 'recomputed': value})
            if m.get('accepted') != len(samples):
                checks.append({'file': rp.name, 'accepted': m.get('accepted'), 'samples': len(samples)})
            for phase in ('telemetry_before', 'telemetry_after'):
                if phase in d:
                    thermal[exp].append(d[phase])
            rss_snapshots.append({'case_id': rp.stem, 'before_rss': d['telemetry_before'].get('rss_bytes'), 'after_rss': d['telemetry_after'].get('rss_bytes'), 'before_lifetime_peak_rss': d['telemetry_before'].get('peak_rss_bytes'), 'after_lifetime_peak_rss': d['telemetry_after'].get('peak_rss_bytes')})
        elif 'original_result' in d:
            o = d['original_result']
            thermal['original_e2'].append(d['telemetry'])
            original_system.append(o['system'])
            samples = o['timing']['raw_acceptance_latencies_ns']
            median = statistics.median(samples)
            if not equal_number(o['timing']['acceptance_ns']['median'], median):
                checks.append({'file': rp.name, 'original_median_mismatch': True})
            if len(samples) != 200 or not all(o['checks'].values()):
                checks.append({'file': rp.name, 'original_acceptance_checks': o['checks'], 'count': len(samples)})
    raw_by_run[name] = {}
    for r in records:
        cid = r['case_id']
        if cid in raw_entries:
            raw_by_run[name][field_signature(r)] = (r, raw_entries[cid], input_cfg.get(cid, {}))
            if 'metrics' in raw_entries[cid] and r['metrics'] != raw_entries[cid]['metrics']:
                checks.append({'case_id': cid, 'record_metrics_differ_from_raw': True})
            if 'original_result' in raw_entries[cid]:
                samples = raw_entries[cid]['original_result']['timing']['raw_acceptance_latencies_ns']
                original_expected = {'queries': len(samples), 'median_ms': statistics.median(samples) / 1000000.0, 'p95_ms': percentile(samples, 0.95) / 1000000.0, 'p99_ms': percentile(samples, 0.99) / 1000000.0}
                for metric, value in original_expected.items():
                    if not equal_number(r['metrics'].get(metric), value):
                        checks.append({'case_id': cid, 'metric': metric, 'record_value': r['metrics'].get(metric), 'recomputed_original': value})
    workers = [d for d in raw_entries.values() if 'raw_samples' in d]
    required_worker_ids = [r['case_id'] for r in records if r['experiment'] in ('local', 'memory_pressure', 'network', 'archive_retrieval', 'energy_workload', 'original_e2') and r['status'] == 'ok']
    missing_worker_ids = [cid for cid in required_worker_ids if cid not in raw_entries or cid not in input_cfg]
    matrix = []
    for key, rr in sorted(groups([r for r in records if r['status'] == 'ok'], lambda r: field_signature(r)[:4] + field_signature(r)[5:]).items(), key=lambda kv: str(kv[0])):
        matrix.append({'group': key, 'records': len(rr), 'trials': sorted({r.get('provenance', {}).get('trial') for r in rr}, key=str), 'query_counts': sorted({r.get('metrics', {}).get('queries') for r in rr}, key=str)})
    real = [r for r in records if r['data_kind'] == 'ethereum_rpc_finalized_json']
    real_signatures = sorted({(r.get('provenance', {}).get('dataset_sha256'), r.get('provenance', {}).get('selected_count'), r.get('provenance', {}).get('selected_payload_sha256')) for r in real}, key=str)
    storage_dataset_signatures = sorted({(r['data_kind'], r['provenance'].get('n'), r['provenance'].get('dataset_sha256'), r['provenance'].get('selected_payload_sha256'), r['metrics'].get('raw_opaque_payload_bytes')) for r in records if r['experiment'] == 'storage'}, key=str)
    ds = base / 'supplied_dataset_manifest.json'
    migration = []
    for path in base.glob('migration/migration-*/records.jsonl'):
        rr = rows(path)
        migration.append({'path': str(path.relative_to(INPUT)), 'records': len(rr), 'status_counts': counter((r.get('status') for r in rr))})
    summary = {'run': name, 'source_directory': str(base.relative_to(INPUT)), 'input_sha256': {'run_manifest.json': digest(mp), 'results.jsonl': digest(base / 'results.jsonl')}, 'complete': bool(manifest.get('completed_unix_ns')), 'manifest_failed_records': manifest.get('failed_records'), 'fatal_error': manifest.get('fatal_error'), 'publication_ready_as_stored': manifest.get('publication_ready'), 'record_counts': counter((r['status'] for r in records)), 'experiment_counts': counter((r['experiment'] + ':' + r['status'] for r in records)), 'not_ok_records': [{k: r.get(k) for k in ('case_id', 'experiment', 'status', 'provenance')} for r in records if r['status'] != 'ok'], 'profile': manifest['profile'], 'config': manifest['config'], 'suite_version': manifest['suite_version'], 'platform': manifest['platform'], 'provider_platform': manifest.get('provider_platform'), 'source_code_sha256': manifest['code_sha256'], 'dataset_manifest': load(ds) if ds.exists() else None, 'real_dataset_record_signatures': real_signatures, 'storage_dataset_signatures': storage_dataset_signatures, 'raw_worker_measurement_count': len(workers), 'raw_original_e2_count': len(original_system), 'raw_metrics_validation_discrepancies': checks, 'missing_worker_files': missing_worker_ids, 'measurement_matrix': matrix, 'thermal_by_experiment': {k: thermal_summary(v) for k, v in thermal.items()}, 'thermal_all_worker_endpoints': thermal_summary([t for ts in thermal.values() for t in ts]), 'lifetime_peak_rss_before_equals_after_count': sum((r['before_lifetime_peak_rss'] == r['after_lifetime_peak_rss'] for r in rss_snapshots)), 'lifetime_peak_rss_caveat': 'resource.ru_maxrss is already large before measurement; /proc endpoint RSS is a point sample and neither field is a sampled phase peak.', 'rss_snapshots': rss_snapshots, 'original_e2_system_first': original_system[0] if original_system else None, 'original_e2_cpu_governors': counter((str(s.get('cpu_governor')) for s in original_system)), 'migration_records': migration}
    all_audits[name] = summary
pairing = []
for left, right in [('pi_a_paper_ethereum_ethernet2', 'pi-a-paper_ethereum_wifi'), ('pi_a_paper_ethereum_ethernet2', 'hpc_real_422175'), ('pi_a_paper_ethereum_ethernet2', 'pi-a-paper_ethereum_ethernet'), ('pi-a-paper_ethereum_ethernet', 'pi-a-paper_ethereum_wifi'), ('pi-a-paper_synthetic_ethernet', 'pi-a-paper_ethereum_wifi'), ('pi-a-paper_synthetic_ethernet', 'hpc_real_422175'), ('pi-a-paper_ethereum_wifi', 'hpc_real_422175')]:
    a, b = (raw_by_run[left], raw_by_run[right])
    common = a.keys() & b.keys()
    bad_positions = [key for key in common if a[key][2].get('positions') != b[key][2].get('positions')]
    bad_original_case = [key for key in common if key[1] == 'original_e2' and a[key][1]['original_result']['case'] != b[key][1]['original_result']['case']]
    ha, hb = (all_audits[left]['source_code_sha256'], all_audits[right]['source_code_sha256'])
    bad_code = [f for f in sorted(ha.keys() | hb.keys()) if ha.get(f) != hb.get(f)]
    pairing.append({'left': left, 'right': right, 'shared_worker_keys': len(common), 'position_mismatches': bad_positions, 'original_case_mismatches': bad_original_case, 'code_hash_differences': bad_code, 'warning': 'Matching workload does not control thermal state, OS, interpreter/OpenSSL, storage, or order effects; each scope is analyzed separately.'})
payload = {'input_root_layout': 'HPC_results/, Pi_results/, ethernet2/', 'runs': all_audits, 'pairing': pairing, 'raw_error_files': errors, 'missing_top_level_hpc_logs': not any(INPUT.glob('HPC_results/**/*.out')), 'policy': 'Failed and hotter runs remain audit inputs; individual samples are retained and repeated runs are not pooled as extra trials.'}
(OUT / 'platform_audit.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
print(json.dumps({'runs': {k: {'complete': v['complete'], 'counts': v['record_counts'], 'raw_discrepancies': len(v['raw_metrics_validation_discrepancies']), 'missing_workers': len(v['missing_worker_files']), 'thermal': v['thermal_all_worker_endpoints']} for k, v in all_audits.items()}, 'pairing': pairing, 'raw_errors': len(errors)}, indent=2))
new_name = 'pi_a_paper_ethereum_ethernet2'
new_dir = INPUT / 'ethernet2' / new_name
new = all_audits[new_name]
new_records = all_records[new_name]
new_workers = raw_by_run[new_name]
recovery = [r for r in new_records if r['experiment'] == 'disconnect']
pressure = [r for r in new_records if r['experiment'] == 'memory_pressure']
integrity = [r for r in new_records if r['experiment'] == 'integrity']
network = [r for r in new_records if r['experiment'] == 'network']
migrations = [r for r in new_records if r['experiment'] == 'migration']
migration_telemetry = [r['metrics'][field] for r in migrations for field in ('snapshot_before', 'snapshot_after') if field in r['metrics']]
provider_urls = sorted({cfg['provider'] for _, _, cfg in new_workers.values() if cfg.get('provider')})
new_pairing = [p for p in pairing if p['left'] == new_name]
payload_hash_groups = new['storage_dataset_signatures']
dataset_comparison = {}
comparison_migration_thermal = {}
for other in ('pi-a-paper_ethereum_wifi', 'hpc_real_422175', 'pi-a-paper_ethereum_ethernet'):
    dataset_comparison[other] = {'export_manifest_identical': new['dataset_manifest'] == all_audits[other]['dataset_manifest'], 'storage_dataset_signatures_identical': payload_hash_groups == all_audits[other]['storage_dataset_signatures']}
    snapshots = [r['metrics'][field] for r in all_records[other] if r['experiment'] == 'migration' for field in ('snapshot_before', 'snapshot_after') if field in r['metrics']]
    comparison_migration_thermal[other] = thermal_summary(snapshots)
synthetic_file = new_dir / 'datasets/synthetic/blocks.jsonl'
synthetic_manifest = load(new_dir / 'datasets/synthetic/manifest.json')
synthetic_rows = rows(synthetic_file)
synthetic_payloads = [base64.b64decode(r['payload_b64'], validate=True) for r in synthetic_rows]
synthetic_validation = {'file_sha256': digest(synthetic_file), 'file_sha256_matches_manifest': digest(synthetic_file) == synthetic_manifest['blocks_sha256'], 'row_count': len(synthetic_rows), 'indices_contiguous': [r['index'] for r in synthetic_rows] == list(range(len(synthetic_rows))), 'payload_bytes': sum(map(len, synthetic_payloads)), 'all_payload_lengths_2048': all((len(b) == 2048 for b in synthetic_payloads))}
summary = {'new_run': new_name, 'complete': new['complete'], 'manifest_failed_records': new['manifest_failed_records'], 'record_counts': new['record_counts'], 'experiment_counts': new['experiment_counts'], 'raw_validation_discrepancies': new['raw_metrics_validation_discrepancies'], 'missing_worker_files': new['missing_worker_files'], 'thermal_worker_and_e2': new['thermal_all_worker_endpoints'], 'thermal_by_experiment': new['thermal_by_experiment'], 'thermal_migration': thermal_summary(migration_telemetry), 'thermal_all_unique_boundary_snapshot_count': new['thermal_all_worker_endpoints']['snapshots'] + len(migration_telemetry), 'pressure': {'records': len(pressure), 'queries': sum((r['metrics']['queries'] for r in pressure)), 'accepted': sum((r['metrics']['accepted'] for r in pressure)), 'all_use_256MiB_AS_and_64MiB_touched_buffer': all((r['metrics']['memory_limit_mib'] == 256 and r['metrics']['pressure_buffer_bytes'] == 67108864 for r in pressure)), 'all_use_pre_delivered_pool': all((r['provenance']['source'] == 'pool' for r in pressure))}, 'integrity': {'records': len(integrity), 'total_subcases': sum((r['metrics']['cases'] for r in integrity)), 'honest': sum((r['metrics']['honest'] for r in integrity)), 'mutations_rejected': sum((sum((v is True for k, v in r['metrics'].items() if k.endswith('_rejected'))) for r in integrity)), 'failures': sum((r['metrics']['failures'] for r in integrity))}, 'network': {'records': len(network), 'queries': sum((r['metrics']['queries'] for r in network)), 'accepted': sum((r['metrics']['accepted'] for r in network)), 'provider_urls': provider_urls, 'labels': counter((r['provenance']['network_kind'] for r in network)), 'route_trace_recorded': False, 'both_interfaces_up_on_client_and_provider': all(({i['name'] for i in host['network']['interfaces'] if i['operstate'] == 'up'} >= {'eth0', 'wlan0'} for host in (new['platform'], new['provider_platform'])))}, 'disconnect': {'records': len(recovery), 'injections': sum((r['metrics']['disconnect_cases'] for r in recovery)), 'detected_safe_failure': sum((r['metrics']['retrieval_failed_safely'] for r in recovery)), 'recovered': sum((r['metrics']['recovered'] for r in recovery)), 'false_accepts': sum((r['metrics']['false_accepts'] for r in recovery)), 'faults': counter((r['provenance']['fault'] for r in recovery)), 'untested': counter((r['provenance']['not_tested'] for r in recovery))}, 'migration': {'records': len(migrations), 'status': counter((r['status'] for r in migrations)), 'all_checks_true': all((all(r['metrics']['checks'].values()) for r in migrations)), 'prepositions': sum((r['metrics']['pre_query_audit']['positions_checked'] for r in migrations)), 'postpositions': sum((r['metrics']['post_query_audit']['positions_checked'] for r in migrations))}, 'matched_comparison': new_pairing, 'dataset_comparison': dataset_comparison, 'comparison_migration_thermal': comparison_migration_thermal, 'synthetic_file_validation': synthetic_validation, 'platform': new['platform'], 'provider_platform': new['provider_platform'], 'raw_input_sha256': new['input_sha256'], 'remaining_scope_limits': ['No continuous temperature monitoring; flags only at saved boundaries, with historical flags still set.', 'No per-query provider thermal evidence or provider source-code hash in health metadata.', 'Both network interfaces were up; the recorded Ethernet run targets 192.168.1.4, but actual route was not independently recorded.', 'Source runtime and governor differences preclude pure hardware attribution in Pi/HPC comparison.', 'Lifetime ru_maxrss already inflated before measurement; report endpoint /proc RSS as sampled, not transient peak.', 'No external energy measurement, native finality, physical power-cut, flash-endurance or RF-loss test.']}
(OUT / 'ethernet2_summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print('ETHERNET2_SUMMARY=' + json.dumps({k: summary[k] for k in ['complete', 'record_counts', 'thermal_worker_and_e2', 'thermal_migration', 'pressure', 'integrity', 'network', 'disconnect', 'migration', 'dataset_comparison', 'synthetic_file_validation']}, sort_keys=True))
