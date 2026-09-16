
from __future__ import annotations
import copy
import json
from pathlib import Path
import random
import resource
import sys
import time
from . import core, metrics
from .service import Client, read_token


def _summary_ns(values):
    return metrics.summarise([v / 1e6 for v in values])


def measure(cfg: dict) -> dict:
    descriptor = core.load_descriptor(Path(cfg['descriptor']))
    pressure = None
    limit = cfg.get('memory_limit_mib', 0)
    if limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit * 1048576, limit * 1048576))
        pressure = bytearray(cfg.get('pressure_mib', 0) * 1048576)
        for i in range(0, len(pressure), 4096):
            pressure[i] = 1
    client = Client(cfg['provider'], read_token(Path(cfg['token_file']))) if cfg.get('provider') else None
    before = metrics.snapshot()
    samples = []
    cpu_samples = []
    provider_samples = []
    transport_bytes = []
    accepted = 0
    source = cfg.get('source', 'pool')
    positions = cfg['positions']
    idle_start = idle_end = start_wall = 0
    j = 0
    measured_start = None
    while j < cfg['warmup'] + cfg['queries'] or (measured_start is not None and time.perf_counter()-measured_start < cfg.get('min_seconds',0)):
        if j == cfg['warmup']:
            idle_start = time.time_ns()
            if cfg.get('idle_seconds', 0):
                time.sleep(cfg['idle_seconds'])
            idle_end = time.time_ns()
            start_wall = time.time_ns()
            measured_start = time.perf_counter()
        position = positions[j % len(positions)]
        
        if source == 'pool':
            response = json.loads((Path(cfg['pool']) / f'{position}.json').read_text())['response']
        start = time.perf_counter_ns()
        if source == 'network':
            package = client.call('/query', {'case_id': cfg['remote_case_id'], 'position': position})
            response = package['response']
        elif source == 'archive_disk':
            package = core.query_store(Path(cfg['case_path']), position)
            response = package['response']
        accept_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        ok = response.get('query') == [descriptor['shard'], descriptor['epoch'], position] and core.verify_response(descriptor, response)
        cpu_ns = time.process_time_ns() - cpu_start
        accept_ns = time.perf_counter_ns() - accept_start
        end = time.perf_counter_ns()
        if not ok:
            raise RuntimeError('honest query rejected or response does not match outstanding request')
        if j >= cfg['warmup']:
            accepted += 1
            samples.append({'position': position, 'elapsed_ns': end-start, 'accept_ns': accept_ns, 'accept_cpu_ns': cpu_ns})
            cpu_samples.append(cpu_ns)
            if source != 'pool':
                provider_samples.append(package['provider'])
            if source == 'network':
                transport_bytes.append(client.last_request_bytes + client.last_response_bytes)
        j += 1
    end_wall = time.time_ns()
    if client:
        client.close()
    after = metrics.snapshot()
    stats = _summary_ns([s['elapsed_ns'] for s in samples])
    accept_stats = _summary_ns([s['accept_ns'] for s in samples])
    result = {'metrics': {'queries': len(samples), 'accepted': accepted,
              'median_ms': stats['median'], 'p95_ms': stats['p95'], 'p99_ms': stats['p99'],
              'accept_median_ms': accept_stats['median'],
              'cpu_ms_per_query': sum(cpu_samples)/len(cpu_samples)/1e6,
              'rss_bytes': after['rss_bytes'], 'peak_rss_bytes': after['peak_rss_bytes'],
              'baseline_rss_bytes': before['rss_bytes'], 'memory_limit_mib': limit,
              'pressure_buffer_bytes': len(pressure) if pressure is not None else 0,
              'measured_duration_seconds': (end_wall-start_wall)/1e9,
              'mean_json_body_bytes': sum(transport_bytes)/len(transport_bytes) if transport_bytes else 0},
              'raw_samples': samples, 'provider_samples': provider_samples,
              'telemetry_before': before, 'telemetry_after': after,
              'window': {'run_id': cfg['case_id'], 'start_unix_ns': start_wall, 'end_unix_ns': end_wall}}
    if idle_end-idle_start > 1000000:
        result['window'].update(idle_start_unix_ns=idle_start, idle_end_unix_ns=idle_end)
    return result


def original(cfg: dict) -> dict:
    from msi_query_exp.benchmark import run_case
    from msi_query_exp.config import make_plan
    config = {'schema_version': 1, 'experiment': 'E2',
        'defaults': {'trial_count': 1, 'measured_queries': cfg['queries'], 'warmup_queries': cfg['warmup'],
                     'phase_queries': 5, 'allocation_queries': 2, 'query_pool_size': min(16,cfg['n']), 'plan_seed': cfg['seed']},
        'sweeps': [{'name': 'same_original_e2', 'factor_grid': {'epoch_length': [cfg['n']]},
                    'case_grid': {'scheme': [cfg['scheme']], 'ablation': ['safe']},
                    'fixed': {'historical_epochs': 1000, 'block_bytes': 2048, 'layout': 'epoch_packed', 'codec': 'raw-v1', 'version': 1}}]}
    plan = make_plan(config)
    result = run_case(plan[0]['cases'][0])
    return {'original_result': result, 'telemetry': metrics.snapshot()}


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[2])
    try:
        value = original(cfg) if cfg['kind'] == 'original' else measure(cfg)
        metrics.atomic_json(output, value)
    except Exception as exc:
        metrics.atomic_json(output, {'error': type(exc).__name__, 'message': str(exc)})
        raise


if __name__ == '__main__':
    main()
