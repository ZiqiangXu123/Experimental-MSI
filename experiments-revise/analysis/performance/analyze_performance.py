#!/usr/bin/env python3
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics as st
RUNS = {'HPC': 'HPC_results/hpc_real_422175', 'Pi_Ethernet': 'ethernet2/pi_a_paper_ethereum_ethernet2', 'Pi_WiFi': 'Pi_results/pi-a-paper_ethereum_wifi'}
EXPERIMENTS = {'local', 'network', 'memory_pressure', 'archive_retrieval', 'original_e2'}

def quantile(v, q):
    v = sorted(v)
    f = (len(v) - 1) * q
    i = int(f)
    return v[i] if i == len(v) - 1 else v[i] + (v[i + 1] - v[i]) * (f - i)

def dump_csv(path, rows):
    keys = list(dict.fromkeys((k for r in rows for k in r)))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input-root', type=Path, default=Path('results_intake'))
    ap.add_argument('--out', type=Path, default=Path(__file__).parent)
    args = ap.parse_args()
    args.input_root, args.out = (args.input_root.resolve(), args.out.resolve())
    if not args.input_root.is_dir():
        ap.error('Input root not found; provide the directory containing extracted result archives.')
    if args.out == args.input_root or args.input_root in args.out.parents:
        ap.error('Derived output must be outside the read-only input tree.')
    args.out.mkdir(exist_ok=True, parents=True)
    trial_rows, telemetry, mismatches, hashed, counts = ([], [], [], {}, {})
    manifests, raws, records_by_run, configs = ({}, {}, {}, {})
    for run, rel in RUNS.items():
        d = args.input_root / rel
        manifests[run] = json.loads((d / 'run_manifest.json').read_text())
        records = [json.loads(x) for x in (d / 'results.jsonl').read_text().splitlines()]
        records_by_run[run] = records
        counts[run] = dict(collections.Counter((r['experiment'] + ':' + r['status'] for r in records)))
        for p in [d / 'run_manifest.json', d / 'results.jsonl']:
            hashed[str(p.relative_to(args.input_root))] = digest(p)
        for r in records:
            if r['experiment'] not in EXPERIMENTS or r['status'] != 'ok':
                continue
            p = d / 'raw' / (r['case_id'] + '.json')
            raw = json.loads(p.read_text())
            hashed[str(p.relative_to(args.input_root))] = digest(p)
            raws[run, r['case_id']] = raw
            cfg_path = d / 'raw' / (r['case_id'] + '.input.json')
            configs[run, r['case_id']] = json.loads(cfg_path.read_text())
            hashed[str(cfg_path.relative_to(args.input_root))] = digest(cfg_path)
            orig = r['experiment'] == 'original_e2'
            pv = r['provenance']
            if orig:
                ns = raw['original_result']['timing']['raw_acceptance_latencies_ns']
                values = [x / 1000000.0 for x in ns]
                if not all(raw['original_result']['checks'].values()):
                    mismatches.append({'run': run, 'case_id': r['case_id'], 'kind': 'original_checks_failed'})
                additional = {}
                stats = raw['original_result']['timing']['acceptance_ns']
                med = st.median(values)
            else:
                s = raw['raw_samples']
                values = [v['elapsed_ns'] / 1000000.0 for v in s]
                med = st.median(values)
                metrics = raw['metrics']
                additional = {'accept_median_ms': st.median((x['accept_ns'] / 1000000.0 for x in s)), 'accept_cpu_mean_ms': st.mean((x['accept_cpu_ns'] / 1000000.0 for x in s)), 'rss_after_MiB': metrics['rss_bytes'] / 1048576 if metrics.get('rss_bytes') else None, 'rss_before_MiB': metrics['baseline_rss_bytes'] / 1048576 if metrics.get('baseline_rss_bytes') else None, 'ru_maxrss_MiB_DO_NOT_USE_AS_PHASE_PEAK': metrics['peak_rss_bytes'] / 1048576, 'memory_limit_MiB': metrics['memory_limit_mib'], 'pressure_buffer_MiB': metrics['pressure_buffer_bytes'] / 1048576, 'accepted': metrics['accepted'], 'mean_json_body_bytes': metrics['mean_json_body_bytes']}
                assert metrics['accepted'] == len(values), (run, r['case_id'])
            result = {'run': run, 'experiment': r['experiment'], 'data_kind': r['data_kind'], 'n': pv['n'], 'mode': pv.get('mode', pv.get('scheme')), 'trial': pv['trial'], 'case_id': r['case_id'], 'queries': len(values), 'median_ms': med, 'p95_ms': quantile(values, 0.95), 'p99_ms': quantile(values, 0.99), **additional, 'selected_payload_sha256': pv.get('selected_payload_sha256', ''), 'timing_scope': pv.get('timing_scope', '')}
            check_keys = {'median_ms': 'median_ms', 'p95_ms': 'p95_ms', 'p99_ms': 'p99_ms'}
            if not orig:
                check_keys.update(accept_median_ms='accept_median_ms', accept_cpu_mean_ms='cpu_ms_per_query')
            for key, record_key in check_keys.items():
                if not math.isclose(result[key], r['metrics'][record_key], rel_tol=1e-12, abs_tol=1e-12):
                    mismatches.append({'run': run, 'case_id': r['case_id'], 'kind': key, 'recomputed': result[key], 'recorded': r['metrics'][record_key]})
            trial_rows.append(result)
        for p in sorted((d / 'raw').glob('*.json')):
            if p.name.endswith('.input.json'):
                continue
            a = json.loads(p.read_text())
            hashed[str(p.relative_to(args.input_root))] = digest(p)
            for phase in ['telemetry_before', 'telemetry_after', 'telemetry']:
                t = a.get(phase)
                if not t:
                    continue
                flag = t.get('throttled')
                bits = int(flag.split('=')[-1], 16) if flag else None
                telemetry.append({'run': run, 'raw_file': p.name, 'phase': phase, 'timestamp_utc': t.get('timestamp_utc'), 'temp_C': t.get('temp_C'), 'throttled': flag, 'active_low_bits': bits & 15 if bits is not None else None, 'rss_MiB': t['rss_bytes'] / 1048576 if t.get('rss_bytes') else None, 'ru_maxrss_MiB': t['peak_rss_bytes'] / 1048576 if t.get('peak_rss_bytes') else None})
    trial_rows.sort(key=lambda r: (r['run'], r['experiment'], r['data_kind'], r['n'], r['mode'], r['trial']))
    groups = collections.defaultdict(list)
    for r in trial_rows:
        groups[r['run'], r['experiment'], r['data_kind'], r['n'], r['mode']].append(r)
    summary = []
    for k, rs in sorted(groups.items()):
        assert {r['trial'] for r in rs} == set(range(5)) and len(rs) == 5, (k, len(rs))
        row = dict(zip(['run', 'experiment', 'data_kind', 'n', 'mode'], k))
        row.update(trials=len(rs), queries_per_trial=rs[0]['queries'])
        for metric in ['median_ms', 'p95_ms', 'p99_ms', 'accept_median_ms', 'accept_cpu_mean_ms', 'rss_after_MiB', 'rss_before_MiB', 'mean_json_body_bytes']:
            vs = [r[metric] for r in rs if r.get(metric) is not None]
            if vs:
                row.update({metric + '_median': st.median(vs), metric + '_min': min(vs), metric + '_max': max(vs)})
        summary.append(row)
    matches = []
    for other in ['HPC', 'Pi_WiFi']:
        for exp in ['local', 'memory_pressure', 'archive_retrieval'] + (['network'] if other == 'Pi_WiFi' else []):
            e = {(r['data_kind'], r['n'], r['mode'], r['trial']): r for r in trial_rows if r['run'] == 'Pi_Ethernet' and r['experiment'] == exp}
            o = {(r['data_kind'], r['n'], r['mode'], r['trial']): r for r in trial_rows if r['run'] == other and r['experiment'] == exp}
            assert e.keys() == o.keys(), (other, exp, 'cohort mismatch')
            for key, p in e.items():
                q = o[key]
                assert p['selected_payload_sha256'] == q['selected_payload_sha256'], (other, exp, key, 'payload mismatch')
                pc = configs['Pi_Ethernet', p['case_id']]
                qc = configs[other, q['case_id']]
                for field in ['positions', 'queries', 'warmup', 'memory_limit_mib', 'pressure_mib', 'idle_seconds', 'source']:
                    assert pc[field] == qc[field], (other, exp, key, field)
                ps = raws['Pi_Ethernet', p['case_id']]['raw_samples']
                qs = raws[other, q['case_id']]['raw_samples']
                assert [s['position'] for s in ps] == [s['position'] for s in qs], (other, exp, key, 'measured position sequence')
            matches.append({'pair': 'Pi_Ethernet vs ' + other, 'experiment': exp, 'matched_trial_cases': len(e), 'payload_hash_match': True, 'positions_warmup_queries_limits_match': True})
    ratios = []
    for pi in ['Pi_Ethernet', 'Pi_WiFi']:
        for n in [512, 4096]:
            for mode in ['B1_verified', 'B2_full', 'B3_leaf', 'B4_ext']:
                pr = groups[pi, 'original_e2', 'synthetic', n, mode]
                hr = groups['HPC', 'original_e2', 'synthetic', n, mode]
                paired = []
                for p, h in zip(pr, hr):
                    pc = raws[pi, p['case_id']]['original_result']['case']
                    hc = raws['HPC', h['case_id']]['original_result']['case']
                    for key in ['fixture_seed', 'query_seed', 'epoch_length', 'scheme', 'measured_queries', 'warmup_queries']:
                        assert pc[key] == hc[key], (pi, n, mode, key)
                    paired.append(p['median_ms'] / h['median_ms'])
                ratios.append({'pi_run': pi, 'n': n, 'scheme': mode, 'trials': 5, 'pi_trial_median_ms': st.median((x['median_ms'] for x in pr)), 'hpc_trial_median_ms': st.median((x['median_ms'] for x in hr)), 'paired_ratio_median': st.median(paired), 'paired_ratio_min': min(paired), 'paired_ratio_max': max(paired)})
    checks = {}
    for run, records in records_by_run.items():
        pressure = [r for r in records if r['experiment'] == 'memory_pressure']
        integrity = [r for r in records if r['experiment'] == 'integrity']
        disconnect = [r for r in records if r['experiment'] == 'disconnect']
        ts = [t for t in telemetry if t['run'] == run]
        temps = [t['temp_C'] for t in ts if t['temp_C'] is not None]
        checks[run] = {'pressure_cases': len(pressure), 'pressure_accepted': sum((r['metrics'].get('accepted', 0) for r in pressure)), 'pressure_queries': sum((r['metrics'].get('queries', 0) for r in pressure)), 'integrity_cases': sum((r['metrics'].get('cases', 0) for r in integrity)), 'integrity_failures': sum((r['metrics'].get('failures', 0) for r in integrity)), 'disconnect_cases': len(disconnect), 'disconnect_false_accepts': sum((r['metrics'].get('false_accepts', 0) for r in disconnect)), 'disconnect_recovered': sum((r['metrics'].get('recovered', False) for r in disconnect)), 'telemetry_count': len(ts), 'throttle_counts': dict(collections.Counter((str(t['throttled']) for t in ts))), 'active_throttle_snapshots': sum((bool(t['active_low_bits']) for t in ts)), 'min_temp_C': min(temps) if temps else None, 'max_temp_C': max(temps) if temps else None, 'record_counts': counts[run]}
    sg = {(r['run'], r['experiment'], r['n'], r['mode']): r for r in summary}
    candidate = []
    for mode in ['archive', 'full', 'leaf', 'ext_cached', 'ext_rebuild']:
        row = {'mode': mode, 'archive_scope_note': 'local disk retrieval + verification; no remote archive baseline' if mode == 'archive' else ''}
        for label, run, n in [('synthetic4096_eth', 'Pi_Ethernet', 4096), ('synthetic4096_wifi', 'Pi_WiFi', 4096), ('ethereum69_eth', 'Pi_Ethernet', 69), ('ethereum69_wifi', 'Pi_WiFi', 69)]:
            experiment = 'archive_retrieval' if mode == 'archive' else 'network'
            r = sg[run, experiment, n, mode]
            row[label + '_source_run'] = run
            row[label + '_scope'] = experiment
            for metric in ['median_ms', 'p95_ms', 'rss_after_MiB']:
                for agg in ['median', 'min', 'max']:
                    row[label + '_' + metric + '_' + agg] = r[metric + '_' + agg]
        r = sg['Pi_WiFi', 'local', 69, mode]
        row['ethereum69_wifi_local_accept_median_ms'] = r['accept_median_ms_median']
        row['ethereum69_wifi_local_accept_cpu_mean_ms'] = r['accept_cpu_mean_ms_median']
        row['ethernet_thermal_selection'] = 'entire new Ethernet synthetic+Ethereum run; all 470 snapshot active bits zero'
        candidate.append(row)
    for name, rows in [('trial_metrics.csv', trial_rows), ('five_trial_summary.csv', summary), ('e2_matched_platform_ratios.csv', ratios), ('thermal_snapshots.csv', telemetry)]:
        dump_csv(args.out / name, rows)
    dump_csv(args.out / 'main_paper_table_candidate.csv', candidate)
    complete = []
    for n, kind in [(512, 'synthetic'), (4096, 'synthetic'), (69, 'ethereum_rpc_finalized_json')]:
        for mode in ['archive', 'full', 'leaf', 'ext_cached', 'ext_rebuild']:
            row = {'data_kind': kind, 'n': n, 'mode': mode}
            for run in ['HPC', 'Pi_Ethernet', 'Pi_WiFi']:
                r = sg[run, 'local', n, mode]
                for metric in ['median_ms', 'p95_ms', 'accept_cpu_mean_ms', 'rss_after_MiB']:
                    for agg in ['median', 'min', 'max']:
                        row[run + '_local_' + metric + '_' + agg] = r[metric + '_' + agg]
                if run == 'HPC':
                    continue
                exp = 'archive_retrieval' if mode == 'archive' else 'network'
                row[run + '_retrieval_scope'] = exp
                r = sg[run, exp, n, mode]
                for metric in ['median_ms', 'p95_ms', 'accept_cpu_mean_ms', 'rss_after_MiB']:
                    for agg in ['median', 'min', 'max']:
                        row[run + '_retrieval_' + metric + '_' + agg] = r[metric + '_' + agg]
            complete.append(row)
    dump_csv(args.out / 'complete_all_modes_three_workloads.csv', complete)
    (args.out / 'cohort_matching.json').write_text(json.dumps(matches, indent=2) + '\n')
    (args.out / 'verification.json').write_text(json.dumps({'checks': checks, 'raw_summary_mismatches': mismatches, 'aggregation': 'Each row aggregates exactly five independent process trials; latency uses median of per-trial medians and range of those medians. P95 uses median of within-trial P95 values.', 'rss_scope': 'Post-query current RSS snapshot; ru_maxrss is not used as a phase peak.', 'network_confound': 'The new complete Ethereum-inclusive Ethernet rerun replaces both previous Ethernet cohorts as primary evidence. All 470 Pi-A telemetry snapshots in each selected Ethernet/WiFi batch have no active throttle flags; histories persist. Radio/thermal conditions remain independent-session observations, not fully controlled causal estimates.', 'ratio_scope': 'Same original E2 code/workload; platform deployment ratio, not pure CPU ratio.'}, indent=2) + '\n')
    (args.out / 'input_hashes.json').write_text(json.dumps(hashed, indent=2) + '\n')
    primary_e2 = [r for r in ratios if r['pi_run'] == 'Pi_Ethernet' and r['n'] == 4096]
    real_network = [r for r in trial_rows if r['run'] == 'Pi_Ethernet' and r['experiment'] == 'network' and (r['n'] == 69)]
    numeric = {'primary_ethernet_run': RUNS['Pi_Ethernet'], 'five_trial_aggregation': 'median of five trial medians; min/max of those medians', 'headline_metrics': {'synthetic4096_E2_full_local_acceptance_ms': next((r['pi_trial_median_ms'] for r in primary_e2 if r['scheme'] == 'B2_full')), 'ethereum69_ethernet_full_retrieval_acceptance_ms': sg['Pi_Ethernet', 'network', 69, 'full']['median_ms_median'], 'ethereum69_wifi_full_retrieval_acceptance_ms': sg['Pi_WiFi', 'network', 69, 'full']['median_ms_median']}, 'ethernet_real69_network_query_end_RSS_MiB': {'min': min((r['rss_after_MiB'] for r in real_network)), 'max': max((r['rss_after_MiB'] for r in real_network)), 'scope': '20 trial-end snapshots across four remote modes; excludes archive and pressure buffer'}, 'primary_E2_4096': primary_e2, 'cohort_matches': matches, 'measurement_check': {'raw_statistic_mismatches': len(mismatches), 'trial_rows': len(trial_rows), 'five_trial_groups': len(summary)}}
    (args.out / 'numeric_summary.json').write_text(json.dumps(numeric, indent=2) + '\n')
    indexed = {(r['experiment'], r['data_kind'], r['n'], r['mode'], r['trial']): r for r in trial_rows if r['run'] == 'Pi_Ethernet'}
    paired = []
    for kind, n in [('synthetic', 512), ('synthetic', 4096), ('ethereum_rpc_finalized_json', 69)]:
        for mode in ['full', 'leaf', 'ext_cached', 'ext_rebuild']:
            v = [indexed['memory_pressure', kind, n, mode, t]['accept_median_ms'] / indexed['local', kind, n, mode, t]['accept_median_ms'] for t in range(5)]
            paired.append({'data_kind': kind, 'n': n, 'mode': mode, 'paired_ratio_median': st.median(v), 'paired_ratio_min': min(v), 'paired_ratio_max': max(v), 'paired_ratios': v})
    pressure = [r for r in trial_rows if r['run'] == 'Pi_Ethernet' and r['experiment'] == 'memory_pressure']
    allratios = [v for r in paired for v in r['paired_ratios']]
    p95 = {r['mode']: r['p95_ms_median'] for r in summary if r['run'] == 'Pi_Ethernet' and r['experiment'] == 'network' and (r['n'] == 69)}
    archive = [{'run': r['run'], 'n': r['n'], 'data_kind': r['data_kind'], 'median_of_trial_medians_ms': r['median_ms_median'], 'min_trial_median_ms': r['median_ms_min'], 'max_trial_median_ms': r['median_ms_max']} for r in summary if r['run'] in ['Pi_Ethernet', 'Pi_WiFi'] and r['experiment'] == 'archive_retrieval']
    mainnotes = {'pressure_scope': 'Ratio of paired trial median acceptance times (accept_ns); post-query RSS includes touched 64 MiB buffer; local pre-delivered-response tests, not network-under-pressure.', 'pressure_by_configuration': paired, 'pressure_configuration_median_ratio_range': [min((r['paired_ratio_median'] for r in paired)), max((r['paired_ratio_median'] for r in paired))], 'pressure_all60_paired_trial_ratio_range': [min(allratios), max(allratios)], 'pressure_query_end_RSS_MiB_range': [min((r['rss_after_MiB'] for r in pressure)), max((r['rss_after_MiB'] for r in pressure))], 'pressure_accepted_queries': sum((r['accepted'] for r in pressure)), 'real69_ethernet_network_median_within_trial_p95_ms': p95, 'real69_ethernet_full_leaf_cache_p95_range_ms': [min((p95[m] for m in ['full', 'leaf', 'ext_cached'])), max((p95[m] for m in ['full', 'leaf', 'ext_cached']))], 'archive_local_retrieval': archive}
    (args.out / 'main_notes.json').write_text(json.dumps(mainnotes, indent=2) + '\n')
    run_summary = {'trials': len(trial_rows), 'groups': len(summary), 'mismatches': len(mismatches), 'checks': checks}
    (args.out / 'run_summary.json').write_text(json.dumps(run_summary, indent=2) + '\n')
    print(json.dumps(run_summary, indent=2))
if __name__ == '__main__':
    main()
