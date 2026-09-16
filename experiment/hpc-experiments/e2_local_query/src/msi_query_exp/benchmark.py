from __future__ import annotations
import gc
import os
import random
import time
import tracemalloc
from pathlib import Path
from typing import Any
from .constants import MODE_BY_SCHEME, PHASES, PRIMARY_PHASES, SCHEMA_VERSION
from .crypto import CycleCounter, OperationCounter, RaplEnergyMeter
from .fixture import build_fixture
from .pipeline import accept_prepared, execute_instrumented, prepare_query
from .util import atomic_write_json, get_peak_rss_kib, get_rss_kib, next_power_of_two, percentile, pin_to_single_cpu, read_cpu_governor, stable_seed, summarise, system_metadata

def _timer_pair_overhead_ns(samples: int=2000) -> dict[str, float]:
    values: list[int] = []
    for _ in range(samples):
        a = time.perf_counter_ns()
        b = time.perf_counter_ns()
        values.append(b - a)
    return {'median': percentile(values, 50), 'p95': percentile(values, 95), 'min': min(values)}

def _query_sequence(pool: tuple[int, ...], count: int, seed: int) -> list[int]:
    rng = random.Random(seed)
    return [pool[rng.randrange(len(pool))] for _ in range(count)]

def _run_no_timing(fixture: Any, case: dict[str, Any], positions: list[int]) -> int:
    accepted = 0
    for position in positions:
        prepared = prepare_query(fixture, position, case)
        if prepared is not None and accept_prepared(prepared, fixture, case) is not None:
            accepted += 1
    return accepted

def _expected_operations(case: dict[str, Any]) -> dict[str, int]:
    n = int(case['epoch_length'])
    n_prime = next_power_of_two(n)
    depth = n_prime.bit_length() - 1
    scheme = str(case['scheme'])
    ablation = str(case.get('ablation', 'safe'))
    if scheme == 'B0_raw':
        return {'hash_calls': 0, 'signature_verifications': 0, 'resolve_hash_calls': 0, 'verify_member_hash_calls': 0}
    if MODE_BY_SCHEME[scheme] == 'leaf':
        resolve_hashes = n_prime
    else:
        resolve_hashes = 0
    member_hashes = depth + 1
    return {'hash_calls': resolve_hashes + member_hashes, 'signature_verifications': 0 if ablation == 'cached_anchor' else 1, 'resolve_hash_calls': resolve_hashes, 'verify_member_hash_calls': member_hashes}

def run_case(case: dict[str, Any]) -> dict[str, Any]:
    affinity = pin_to_single_cpu()
    selected_cpu = affinity.get('selected_cpu')
    system = system_metadata()
    system['affinity'] = affinity
    system['cpu_governor'] = read_cpu_governor(selected_cpu if isinstance(selected_cpu, int) else None)
    system['python_hash_seed'] = os.environ.get('PYTHONHASHSEED')
    system['energy_exclusive_asserted'] = os.environ.get('E2_ENERGY_EXCLUSIVE') == '1'
    system['benchmark_exclusive_requested'] = os.environ.get('E2_BENCHMARK_EXCLUSIVE') == '1'
    fixture = build_fixture(case)
    pool = fixture.target_position_pool
    warmup_queries = int(case['warmup_queries'])
    measured_queries = int(case['measured_queries'])
    phase_queries = int(case['phase_queries'])
    allocation_queries = int(case['allocation_queries'])
    query_seed = int(case['query_seed'])
    warm_positions = _query_sequence(pool, warmup_queries, stable_seed(query_seed, 'warmup'))
    measured_positions = _query_sequence(pool, measured_queries, stable_seed(query_seed, 'measured'))
    phase_positions = _query_sequence(pool, phase_queries, stable_seed(query_seed, 'phase'))
    allocation_positions = _query_sequence(pool, allocation_queries, stable_seed(query_seed, 'allocation'))
    timer_overhead = _timer_pair_overhead_ns()
    first_position = measured_positions[0]
    first_t0 = time.perf_counter_ns()
    first_prepared = prepare_query(fixture, first_position, case)
    first_t1 = time.perf_counter_ns()
    first_result = accept_prepared(first_prepared, fixture, case) if first_prepared is not None else None
    first_t2 = time.perf_counter_ns()
    if first_result is None:
        raise RuntimeError('valid first query was rejected')
    warm_accepted = _run_no_timing(fixture, case, warm_positions)
    if warm_accepted != warmup_queries:
        raise RuntimeError(f'warm-up accepted {warm_accepted}/{warmup_queries}; valid fixtures must all accept')
    gc.collect()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    cycle_counter = CycleCounter()
    energy_meter = RaplEnergyMeter()
    cycle_error: str | None = None
    energy_error: str | None = None
    local_latencies: list[int] = []
    acceptance_latencies: list[int] = []
    prepare_latencies: list[int] = []
    accepted = 0
    cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    try:
        try:
            cycle_counter.start()
        except OSError as exc:
            cycle_error = f'cycle start failed: {exc}'
        if energy_meter.available():
            try:
                energy_meter.start()
            except OSError as exc:
                energy_error = f'energy start failed: {exc}'
        for position in measured_positions:
            t0 = time.perf_counter_ns()
            prepared = prepare_query(fixture, position, case)
            t1 = time.perf_counter_ns()
            result = accept_prepared(prepared, fixture, case) if prepared is not None else None
            t2 = time.perf_counter_ns()
            if result is not None:
                accepted += 1
            prepare_ns = t1 - t0
            local_ns = t2 - t0
            primary_ns = local_ns if case['scheme'] == 'B0_raw' else t2 - t1
            prepare_latencies.append(prepare_ns)
            local_latencies.append(local_ns)
            acceptance_latencies.append(primary_ns)
        wall_end = time.perf_counter_ns()
        cpu_end = time.process_time_ns()
        try:
            cycles = cycle_counter.stop()
        except OSError as exc:
            cycles = None
            cycle_error = f'cycle stop failed: {exc}'
        if energy_meter.available() and energy_error is None:
            try:
                joules = energy_meter.stop()
            except (OSError, RuntimeError) as exc:
                joules = None
                energy_error = f'energy stop failed: {exc}'
        else:
            joules = None
    finally:
        if gc_was_enabled:
            gc.enable()
        cycle_counter.close()
    if accepted != measured_queries:
        raise RuntimeError(f'valid measured queries accepted {accepted}/{measured_queries}')
    phase_raw: dict[str, list[int]] = {phase: [] for phase in PHASES}
    for position in phase_positions:
        ok, phases = execute_instrumented(fixture, position, case)
        if not ok:
            raise RuntimeError('valid phase-sampling query was rejected')
        for phase in PHASES:
            phase_raw[phase].append(int(phases[phase]))
    operation_counter = OperationCounter()
    audited = prepare_query(fixture, measured_positions[-1], case)
    if audited is None or accept_prepared(audited, fixture, case, operation_counter) is None:
        raise RuntimeError('operation-audit query was rejected')
    expected = _expected_operations(case)
    tracemalloc.start()
    tracemalloc.reset_peak()
    trace_before, _ = tracemalloc.get_traced_memory()
    trace_accepted = _run_no_timing(fixture, case, allocation_positions)
    trace_current, trace_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if trace_accepted != allocation_queries:
        raise RuntimeError('allocation pass rejected a valid query')
    wall_ns = wall_end - wall_start
    cpu_ns = cpu_end - cpu_start
    throughput_qps = measured_queries * 1000000000.0 / wall_ns
    cycles_per_query = cycles / measured_queries if cycles is not None else None
    joules_per_query = joules / measured_queries if joules is not None else None
    energy_publication_valid = bool(joules is not None and system['energy_exclusive_asserted'])
    phase_summaries = {phase: summarise(values) for phase, values in phase_raw.items()}
    primary_phase_medians = [float(phase_summaries[p].get('median', 0.0)) for p in PRIMARY_PHASES]
    checks = {'all_measured_queries_accepted': accepted == measured_queries, 'operation_hash_count_exact': operation_counter.hash_calls == expected['hash_calls'], 'signature_count_exact': operation_counter.signature_verifications == expected['signature_verifications'], 'resolve_hash_count_exact': operation_counter.by_phase_hash_calls.get('resolve_witness', 0) == expected['resolve_hash_calls'], 'member_hash_count_exact': operation_counter.by_phase_hash_calls.get('verify_member', 0) == expected['verify_member_hash_calls'], 'phase_samples_accepted': all((len(v) == phase_queries for v in phase_raw.values())), 'primary_latency_positive': min(acceptance_latencies) >= 0}
    if not all(checks.values()):
        failed = [name for name, ok in checks.items() if not ok]
        raise RuntimeError(f'internal E2 audit failed: {failed}')
    result = {'schema_version': SCHEMA_VERSION, 'experiment': 'E2', 'case': case, 'system': system, 'fixture': fixture.setup_metadata, 'timing': {'timer_pair_overhead_ns': timer_overhead, 'first_query': {'prepare_ns': first_t1 - first_t0, 'acceptance_ns': first_t2 - first_t0 if case['scheme'] == 'B0_raw' else first_t2 - first_t1, 'local_query_ns': first_t2 - first_t0}, 'prepare_ns': summarise(prepare_latencies), 'acceptance_ns': summarise(acceptance_latencies), 'local_query_ns': summarise(local_latencies), 'batch_wall_ns': wall_ns, 'batch_cpu_ns': cpu_ns, 'throughput_qps': throughput_qps, 'cpu_utilisation_ratio': cpu_ns / wall_ns if wall_ns else None, 'raw_acceptance_latencies_ns': acceptance_latencies, 'raw_local_query_latencies_ns': local_latencies}, 'phases': {'raw_ns': phase_raw, 'summary_ns': phase_summaries, 'sum_primary_phase_medians_ns': sum(primary_phase_medians)}, 'operations': {'observed': {'hash_calls_per_query': operation_counter.hash_calls, 'signature_verifications_per_query': operation_counter.signature_verifications, 'hash_calls_by_phase': operation_counter.by_phase_hash_calls, 'python_visible_allocated_bytes_per_query': operation_counter.allocated_bytes, 'python_visible_allocations_per_query': operation_counter.allocations, 'allocated_bytes_by_phase': operation_counter.by_phase_allocated_bytes}, 'expected': expected}, 'resources': {'rss_after_fixture_kib': fixture.setup_metadata.get('rss_after_kib'), 'rss_after_benchmark_kib': get_rss_kib(), 'peak_rss_kib': get_peak_rss_kib(), 'tracemalloc_queries': allocation_queries, 'tracemalloc_current_delta_bytes': trace_current - trace_before, 'tracemalloc_peak_delta_bytes': trace_peak - trace_before, 'cycle_counter': {**cycle_counter.metadata(), 'runtime_error': cycle_error, 'aggregate': cycles, 'per_query': cycles_per_query}, 'energy': {**energy_meter.metadata(), 'runtime_error': energy_error, 'aggregate_joules': joules, 'joules_per_query': joules_per_query, 'publication_valid': energy_publication_valid, 'validity_rule': 'RAPL readable and E2_ENERGY_EXCLUSIVE=1 asserted for an exclusive node/job'}}, 'checks': checks}
    try:
        fixture.anchor_backend.close()
    except Exception:
        pass
    return result

def result_filename(case: dict[str, Any]) -> str:
    return f"{case['case_id']}.json.gz"

def write_case_result(case: dict[str, Any], output_dir: Path) -> Path:
    result = run_case(case)
    path = output_dir / result_filename(case)
    atomic_write_json(path, result, compress=True)
    return path
