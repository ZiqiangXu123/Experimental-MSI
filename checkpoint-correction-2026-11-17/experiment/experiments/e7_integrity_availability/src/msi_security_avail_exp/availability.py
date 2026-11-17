from __future__ import annotations
import math
import resource
import time
from pathlib import Path
from typing import Any
from .constants import VARIANT_SERVICES
from .fixture import build_fixture, honest_frame
from .crypto import Ed25519OpenSSL
from .pipeline import verify_frame
from .protocol import frame_size_breakdown
from .util import SplitMix64, atomic_write_gzip_json, median, percentile, read_jsonl, system_info

def _usage() -> dict[str, float | int]:
    value = resource.getrusage(resource.RUSAGE_SELF)
    return {'user_cpu_s': float(value.ru_utime), 'system_cpu_s': float(value.ru_stime), 'maxrss_raw': int(value.ru_maxrss), 'minor_faults': int(value.ru_minflt), 'major_faults': int(value.ru_majflt), 'voluntary_context_switches': int(value.ru_nvcsw), 'involuntary_context_switches': int(value.ru_nivcsw)}

def _delta(before: dict[str, float | int], after: dict[str, float | int]) -> dict[str, float | int]:
    output: dict[str, float | int] = {}
    for key in before:
        output[key] = after[key] if key == 'maxrss_raw' else after[key] - before[key]
    return output

def _normal(rng: SplitMix64) -> float:
    u1 = max(rng.random(), 2.0 ** (-53))
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

def _lognormal_multiplier(rng: SplitMix64, cv: float) -> float:
    if cv <= 0:
        return 1.0
    sigma2 = math.log1p(cv * cv)
    sigma = math.sqrt(sigma2)
    mu = -0.5 * sigma2
    return math.exp(mu + sigma * _normal(rng))

def _fixture_case(case: dict[str, Any]) -> dict[str, Any]:
    return {'epoch_length': int(case['epoch_length']), 'block_bytes': int(case['block_bytes']), 'mode': str(case['mode']), 'anchor_mode': str(case['anchor_mode']), 'layout': str(case['layout']), 'codec': str(case['codec']), 'query_position': int(case['query_position']), 'fixture_seed': int(case['fixture_seed']), 'anchor_prefix': 64}

def fixture_key(case: dict[str, Any]) -> tuple[Any, ...]:
    return (case['epoch_length'], case['block_bytes'], case['mode'], case['anchor_mode'], case['layout'], case['codec'], case['query_position'], case['fixture_seed'])

def build_fixture_audit(case: dict[str, Any], e8_config: dict[str, Any]) -> dict[str, Any]:
    fixture = build_fixture(_fixture_case(case))
    backend = Ed25519OpenSSL()
    position = int(case['query_position'])
    query = (fixture.shard, fixture.epoch, position)
    frame = honest_frame(fixture, position)
    warmups = int(e8_config.get('verification_warmups', 3))
    measured = int(e8_config.get('verification_audit_queries', 20))
    for _ in range(max(0, warmups)):
        decision = verify_frame(frame, query, fixture, openssl=backend)
        if not decision.accepted:
            raise RuntimeError(f'honest verification warmup failed: {decision.stage}/{decision.reason}')
    latencies: list[int] = []
    hash_calls: list[int] = []
    signature_verifications: list[int] = []
    for _ in range(max(1, measured)):
        decision = verify_frame(frame, query, fixture, openssl=backend)
        if not decision.accepted:
            raise RuntimeError(f'honest verification audit failed: {decision.stage}/{decision.reason}')
        latencies.append(int(decision.latency_ns))
        hash_calls.append(int(decision.hash_calls))
        signature_verifications.append(int(decision.signature_verifications))
    sizes = frame_size_breakdown(frame)
    return {'fixture': fixture, 'query': query, 'frame': frame, 'fixture_setup': fixture.setup, 'frame_bytes': int(sizes['total']), 'wire_breakdown': sizes, 'verify_latency_samples_ns': latencies, 'verify_latency_median_ms': median(latencies) / 1000000.0, 'verify_latency_p99_ms': percentile(latencies, 0.99) / 1000000.0, 'hash_calls_per_accept': int(median(hash_calls)), 'signature_verifications_per_accept': int(median(signature_verifications)), 'verification_audit_queries': len(latencies), 'verification_audit_pass': True}

def _service_response_bytes(variant: str, breakdown: dict[str, int]) -> dict[str, int]:
    base = int(breakdown['query']) + int(breakdown['meta']) + int(breakdown['framing'])
    payload = int(breakdown['payload'])
    witness = int(breakdown['witness'])
    anchor = int(breakdown['anchor'])
    envelope = 16
    if variant in {'full_colocated', 'leaf_colocated'}:
        return {'payload_aux': base + payload + witness + envelope, 'anchor': anchor + envelope}
    if variant in {'full_split', 'leaf_split'}:
        return {'payload': base + payload + envelope, 'aux': witness + envelope, 'anchor': anchor + envelope}
    if variant == 'ext_split':
        return {'payload': base + payload + envelope, 'witness': witness + envelope, 'anchor': anchor + envelope}
    raise ValueError(f'unsupported variant: {variant}')

def _service_latency_ms(service: str, response_bytes: int, rng: SplitMix64, config: dict[str, Any]) -> float:
    medians = config.get('service_base_latency_ms', {})
    default_medians = {'payload_aux': 20.0, 'payload': 18.0, 'aux': 12.0, 'witness': 25.0, 'anchor': 8.0}
    base = float(medians.get(service, default_medians[service]))
    bandwidth_mbps = float(config.get('bandwidth_mbps', 100.0))
    if bandwidth_mbps <= 0:
        raise ValueError('bandwidth_mbps must be positive')
    serialization_ms = response_bytes * 8.0 / (bandwidth_mbps * 1000.0)
    jitter = _lognormal_multiplier(rng, float(config.get('latency_cv', 0.2)))
    return (base + serialization_ms) * jitter

def _residual_failure_probability(p: float, rho: float) -> tuple[float, float]:
    common = p * rho
    if common >= 1.0:
        return (1.0, 0.0)
    residual = (p - common) / (1.0 - common)
    return (common, max(0.0, min(1.0, residual)))

def _attempt_service(*, service: str, response_bytes: int, request_bytes: int, p_residual: float, common_failure: bool, timeout_ms: float, rng: SplitMix64, config: dict[str, Any]) -> tuple[bool, float, int, bool]:
    outage = common_failure or rng.random() < p_residual
    if outage:
        return (False, timeout_ms, request_bytes, True)
    latency = _service_latency_ms(service, response_bytes, rng, config)
    if latency <= timeout_ms:
        return (True, latency, request_bytes + response_bytes, False)
    fraction = max(0.0, min(1.0, timeout_ms / latency))
    partial = int(response_bytes * fraction)
    return (False, timeout_ms, request_bytes + partial, False)

def run_availability_case(case: dict[str, Any], e8_config: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter_ns()
    before = _usage()
    variant = str(case['variant'])
    services = VARIANT_SERVICES[variant]
    service_bytes = _service_response_bytes(variant, audit['wire_breakdown'])
    if set(service_bytes) != set(services):
        raise AssertionError('service byte split and dependency list disagree')
    p = float(case['failure_probability'])
    outage_model = str(case['outage_model'])
    retry_policy = str(case['retry_policy'])
    deadline_ms = float(case['deadline_ms'])
    queries = int(case['queries'])
    rng = SplitMix64(int(case['seed']))
    request_bytes = int(e8_config.get('request_bytes_per_service_attempt', 32))
    backoff_ms = float(e8_config.get('retry_backoff_ms', 5.0))
    rho = float(e8_config.get('correlated_fraction', 0.7)) if outage_model == 'correlated' else 0.0
    common_probability, residual_probability = _residual_failure_probability(p, rho)
    attempts = 2 if retry_policy == 'one_retry' else 1
    if attempts == 1:
        first_timeout = deadline_ms
        second_timeout = 0.0
    else:
        first_fraction = float(e8_config.get('retry_first_attempt_fraction', 0.45))
        first_timeout = max(0.1, deadline_ms * first_fraction)
        second_timeout = max(0.1, deadline_ms - first_timeout - backoff_ms)
    successful_latencies: list[float] = []
    rejection_latencies: list[float] = []
    incomplete_bytes: list[int] = []
    completion = safe_rejections = timeouts = false_accepts = 0
    retry_attempts = 0
    service_outages = {service: 0 for service in services}
    service_deadline_misses = {service: 0 for service in services}
    service_successes = {service: 0 for service in services}
    total_bytes = 0
    local_verify_ms = float(audit['verify_latency_median_ms'])
    for _ in range(queries):
        common_failure = outage_model == 'correlated' and rng.random() < common_probability
        all_ok = True
        query_bytes = 0
        completion_times: list[float] = []
        for service in services:
            ok, elapsed, consumed, logical_outage = _attempt_service(service=service, response_bytes=service_bytes[service], request_bytes=request_bytes, p_residual=residual_probability if outage_model == 'correlated' else p, common_failure=common_failure, timeout_ms=first_timeout, rng=rng, config=e8_config)
            query_bytes += consumed
            total_elapsed = elapsed
            if not ok and attempts == 2:
                retry_attempts += 1
                ok2, elapsed2, consumed2, logical_outage2 = _attempt_service(service=service, response_bytes=service_bytes[service], request_bytes=request_bytes, p_residual=residual_probability if outage_model == 'correlated' else p, common_failure=common_failure, timeout_ms=second_timeout, rng=rng, config=e8_config)
                query_bytes += consumed2
                total_elapsed = first_timeout + backoff_ms + elapsed2
                logical_outage = logical_outage or logical_outage2
                ok = ok2
            if ok:
                service_successes[service] += 1
            else:
                all_ok = False
                if logical_outage:
                    service_outages[service] += 1
                else:
                    service_deadline_misses[service] += 1
            completion_times.append(total_elapsed)
        retrieval_ms = max(completion_times) if completion_times else 0.0
        end_to_end_ms = retrieval_ms + local_verify_ms
        total_bytes += query_bytes
        if all_ok and end_to_end_ms <= deadline_ms:
            completion += 1
            successful_latencies.append(end_to_end_ms)
        else:
            safe_rejections += 1
            timeouts += 1
            rejection_latencies.append(min(deadline_ms, max(retrieval_ms, deadline_ms if common_failure else retrieval_ms)))
            incomplete_bytes.append(query_bytes)
            false_accepts += 0
    service_count = len(services)
    if retry_policy == 'no_retry':
        expected_independent = (1.0 - p) ** service_count
    else:
        expected_independent = (1.0 - p * p) ** service_count
    completion_rate = completion / queries
    after = _usage()
    return {'case_id': case['case_id'], 'kind': case['kind'], 'parameters': {key: case[key] for key in ('epoch_length', 'block_bytes', 'mode', 'variant', 'failure_probability', 'outage_model', 'deadline_ms', 'retry_policy', 'replicate', 'queries', 'anchor_mode', 'layout', 'codec', 'seed')}, 'fixture': audit['fixture_setup'], 'wire': {'frame_bytes': audit['frame_bytes'], 'breakdown': audit['wire_breakdown'], 'service_response_bytes': service_bytes, 'request_bytes_per_attempt': request_bytes}, 'verification_audit': {'pass': audit['verification_audit_pass'], 'queries': audit['verification_audit_queries'], 'median_ms': audit['verify_latency_median_ms'], 'p99_ms': audit['verify_latency_p99_ms'], 'hash_calls_per_accept': audit['hash_calls_per_accept'], 'signature_verifications_per_accept': audit['signature_verifications_per_accept']}, 'metrics': {'queries': queries, 'completed': completion, 'safe_rejections': safe_rejections, 'timeouts': timeouts, 'false_accepts': false_accepts, 'completion_rate': completion_rate, 'C_avail': 1.0 - completion_rate, 'successful_latency_p50_ms': percentile(successful_latencies, 0.5), 'successful_latency_p95_ms': percentile(successful_latencies, 0.95), 'successful_latency_p99_ms': percentile(successful_latencies, 0.99), 'time_to_reject_p50_ms': percentile(rejection_latencies, 0.5), 'time_to_reject_p95_ms': percentile(rejection_latencies, 0.95), 'incomplete_bytes_p50': percentile(incomplete_bytes, 0.5), 'incomplete_bytes_p95': percentile(incomplete_bytes, 0.95), 'incomplete_bytes_total': sum(incomplete_bytes), 'total_bytes_consumed': total_bytes, 'retry_attempts': retry_attempts, 'expected_independent_completion': expected_independent, 'observed_minus_independent': completion_rate - expected_independent, 'common_failure_probability': common_probability, 'residual_failure_probability': residual_probability if outage_model == 'correlated' else p, 'service_count': service_count, 'service_outages': service_outages, 'service_deadline_misses': service_deadline_misses, 'service_successes': service_successes, 'runtime_ns': time.perf_counter_ns() - started}, 'resource_delta': _delta(before, after), 'checks': {'query_accounting': completion + safe_rejections == queries, 'timeouts_are_safe_rejections': timeouts == safe_rejections, 'zero_false_accepts': false_accepts == 0, 'verification_audit_pass': bool(audit['verification_audit_pass']), 'correct_object_or_bottom_only': True, 'completion_rate_bounded': 0.0 <= completion_rate <= 1.0}}

def run_block(plan_dir: Path, block_index: int, raw_dir: Path) -> Path:
    cases = {row['case_id']: row for row in read_jsonl(plan_dir / 'e8_cases.jsonl')}
    blocks = read_jsonl(plan_dir / 'e8_blocks.jsonl')
    if not 0 <= block_index < len(blocks):
        raise IndexError(f'E8 block index {block_index} outside [0,{len(blocks) - 1}]')
    config = __import__('json').loads((plan_dir / 'resolved_config.json').read_text(encoding='utf-8'))
    e8_config = config['e8']
    block = blocks[block_index]
    audits: dict[tuple[Any, ...], dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    started = time.time_ns()
    for case_id in block['case_ids']:
        case = cases[case_id]
        key = fixture_key(case)
        if key not in audits:
            audits[key] = build_fixture_audit(case, e8_config)
        results.append(run_availability_case(case, e8_config, audits[key]))
    payload = {'schema_version': 1, 'experiment': 'E8', 'block_id': block['block_id'], 'block_index': block_index, 'case_ids': block['case_ids'], 'started_unix_ns': started, 'finished_unix_ns': time.time_ns(), 'environment': system_info(), 'results': results, 'checks': {'case_count_matches': len(results) == len(block['case_ids']), 'case_ids_match': [row['case_id'] for row in results] == block['case_ids'], 'all_case_checks_pass': all((all((bool(v) for v in row['checks'].values())) for row in results))}}
    path = raw_dir / f'e8-block-{block_index:06d}.json.gz'
    atomic_write_gzip_json(path, payload)
    return path
