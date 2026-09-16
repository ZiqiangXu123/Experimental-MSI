from __future__ import annotations
import gc
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from .constants import FRAME_HEADER_BYTES, MODE_BY_SCHEME, MSG_CONFIG_ACK, MSG_PONG, MSG_QUERY_COALESCED, MSG_QUERY_PAYLOAD, MSG_QUERY_WITNESS
from .crypto import OperationCounter
from .fixture import build_server_fixture, build_verifier_context
from .link import LinkProfile, apply_transfer_delay, sample_transfer
from .pipeline import accept_response
from .util import atomic_write_json, get_peak_rss_kib, get_rss_kib, next_power_of_two, pin_to_single_cpu, read_json, stable_seed, summarise, system_metadata
from .wire import WireBreakdown, add_breakdowns, decode_coalesced_response, decode_frame_bytes, decode_payload_response, decode_witness_response, encode_coalesced_response, encode_config, encode_payload_response, encode_ping, encode_query, encode_witness_response, merge_split_response, recv_frame, send_frame

def case_variant(case: dict[str, Any]) -> str:
    scheme = str(case['scheme'])
    deployment = str(case.get('deployment', 'coalesced'))
    if scheme == 'B4_ext' and deployment == 'split':
        return 'B4_ext_split'
    if scheme == 'B4_ext':
        return 'B4_ext_coalesced'
    return scheme

def result_filename(case: dict[str, Any]) -> str:
    return f"{case['case_id']}.json.gz"

def server_metrics_filename(case: dict[str, Any], role: str) -> str:
    return f"{case['case_id']}.{role}.server.json.gz"

def _query_sequence(pool: tuple[int, ...], count: int, seed: int) -> list[int]:
    rng = random.Random(seed)
    return [pool[rng.randrange(len(pool))] for _ in range(count)]

def _connect(endpoint: dict[str, Any], timeout_s: float) -> socket.socket:
    sock = socket.create_connection((str(endpoint['host']), int(endpoint['port'])), timeout=timeout_s)
    sock.settimeout(timeout_s)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock

def _calibrate(sock: socket.socket, pings: int) -> dict[str, Any]:
    samples: list[int] = []
    for i in range(pings):
        frame = encode_ping(i)
        t0 = time.perf_counter_ns()
        send_frame(sock, frame)
        response = recv_frame(sock)
        t1 = time.perf_counter_ns()
        if response.message_type != MSG_PONG:
            raise RuntimeError('service did not return PONG during calibration')
        samples.append(t1 - t0)
    baseline = int(statistics.median(samples))
    send_frame(sock, encode_config(baseline))
    ack = recv_frame(sock)
    if ack.message_type != MSG_CONFIG_ACK:
        raise RuntimeError('service did not acknowledge calibrated baseline')
    return {'samples_ns': samples, 'median_ns': baseline, 'summary_ns': summarise(samples)}

def _expected_operations(case: dict[str, Any]) -> dict[str, int]:
    n_prime = next_power_of_two(int(case['epoch_length']))
    depth = n_prime.bit_length() - 1
    if case['scheme'] == 'B3_leaf':
        hashes = n_prime + depth + 1
    else:
        hashes = depth + 1
    return {'hash_calls': hashes, 'signature_verifications': 1}

def _wire_audit(case: dict[str, Any]) -> dict[str, Any]:
    server = build_server_fixture(case)
    verifier = build_verifier_context(case)
    position = server.target_position_pool[0]
    response = server.responses[position]
    q = response.q
    deployment = str(case.get('deployment', 'coalesced'))
    request_id = 1
    if deployment == 'coalesced':
        request_frame = encode_query(MSG_QUERY_COALESCED, request_id, q, str(case['scheme']))
        response_frame, breakdown = encode_coalesced_response(request_id, server.entry, response)
        decoded_id, decoded, meta = decode_coalesced_response(decode_frame_bytes(response_frame))
        if decoded_id != request_id:
            raise RuntimeError('coalesced request ID did not round-trip')
        total = WireBreakdown(**{**breakdown.__dict__, 'request_bytes': len(request_frame)})
    else:
        payload_request = encode_query(MSG_QUERY_PAYLOAD, request_id, q, str(case['scheme']))
        witness_request = encode_query(MSG_QUERY_WITNESS, request_id, q, str(case['scheme']))
        payload_frame, b_payload = encode_payload_response(request_id, server.entry, response)
        witness_frame, b_witness = encode_witness_response(request_id, server.entry, response)
        payload_part = decode_payload_response(decode_frame_bytes(payload_frame))
        witness_part = decode_witness_response(decode_frame_bytes(witness_frame))
        decoded_id, decoded, meta = merge_split_response(payload_part, witness_part)
        if decoded_id != request_id:
            raise RuntimeError('split request ID did not round-trip')
        total = add_breakdowns(b_payload, b_witness, request_bytes=len(payload_request) + len(witness_request))
    accepted = accept_response(verifier, decoded, case)
    if accepted is None:
        raise RuntimeError('wire-audit response failed local verification')
    n_prime = next_power_of_two(int(case['epoch_length']))
    depth = n_prime.bit_length() - 1
    expected_witness_bytes = 5 + (32 * n_prime if case['scheme'] == 'B3_leaf' else 33 * depth)
    checks = {'roundtrip_accepted': accepted is not None, 'witness_bytes_exact': total.witness_bytes == expected_witness_bytes, 'response_accounting_exact': total.total_response_bytes == total.payload_bytes + total.witness_bytes + total.anchor_evidence_bytes + total.metadata_bytes + total.framing_bytes, 'request_bytes_positive': total.request_bytes > 0}
    if not all(checks.values()):
        raise RuntimeError(f'wire audit failed: {[k for k, v in checks.items() if not v]}')
    try:
        verifier.anchor_backend.close()
    except Exception:
        pass
    return {'schema_version': 1, 'experiment': 'E3', 'kind': 'wire_audit', 'case': case, 'variant': case_variant(case), 'system': system_metadata(), 'fixture': server.setup_metadata, 'wire': total.as_dict(), 'expected': {'n_prime': n_prime, 'depth': depth, 'witness_bytes': expected_witness_bytes}, 'checks': checks}

def _one_transaction(sock: socket.socket, *, case: dict[str, Any], role: str, request_type: int, request_id: int, q: tuple[int, int, int], baseline_rtt_ns: int, transaction_index: int) -> tuple[Any, dict[str, Any]]:
    profile = LinkProfile.from_id(str(case['profile']))
    serialize_t0 = time.perf_counter_ns()
    request_frame = encode_query(request_type, request_id, q, str(case['scheme']))
    serialize_t1 = time.perf_counter_ns()
    request_sample = sample_transfer(profile, len(request_frame), baseline_rtt_ns=baseline_rtt_ns, seed=int(case['network_seed']), endpoint=role, direction='request', request_id=request_id, transaction_index=transaction_index)
    applied_request = apply_transfer_delay(request_sample)
    send_t0 = time.perf_counter_ns()
    send_frame(sock, request_frame)
    send_t1 = time.perf_counter_ns()
    receive_t0 = time.perf_counter_ns()
    response_frame = recv_frame(sock)
    receive_t1 = time.perf_counter_ns()
    return (response_frame, {'role': role, 'request_frame_bytes': len(request_frame), 'response_frame_bytes': response_frame.total_bytes, 'request_serialize_ns': serialize_t1 - serialize_t0, 'request_socket_send_ns': send_t1 - send_t0, 'response_socket_wait_and_receive_ns': receive_t1 - receive_t0, 'request_link': applied_request.as_dict()})

def run_network_case(case: dict[str, Any], endpoints: dict[str, dict[str, Any]], server_metric_files: dict[str, str]) -> dict[str, Any]:
    system = system_metadata()
    system['affinity'] = pin_to_single_cpu()
    context = build_verifier_context(case)
    deployment = str(case.get('deployment', 'coalesced'))
    profile = LinkProfile.from_id(str(case['profile']))
    timeout_s = float(case.get('socket_timeout_s', 600.0))
    calibration_pings = int(case.get('calibration_pings', 15))
    sockets: dict[str, socket.socket] = {}
    calibration: dict[str, Any] = {}
    roles = ['gateway'] if deployment == 'coalesced' else ['payload', 'witness']
    try:
        for role in roles:
            sockets[role] = _connect(endpoints[role], timeout_s)
            calibration[role] = _calibrate(sockets[role], calibration_pings)
        warmup = int(case['warmup_queries'])
        measured = int(case['measured_queries'])
        warm_positions = _query_sequence(context.target_position_pool, warmup, stable_seed(case['query_seed'], 'warmup'))
        measured_positions = _query_sequence(context.target_position_pool, measured, stable_seed(case['query_seed'], 'measured'))
        last_response = None
        for i, position in enumerate(warm_positions):
            request_id = 1000000 + i
            response, _record = _execute_query(case, context, sockets, calibration, request_id, position, record=False)
            if accept_response(context, response, case) is None:
                raise RuntimeError('valid warm-up response was rejected')
            last_response = response
        gc.collect()
        raw_queries: list[dict[str, Any]] = []
        accepted = 0
        batch_t0 = time.perf_counter_ns()
        cpu_t0 = time.process_time_ns()
        for i, position in enumerate(measured_positions):
            request_id = 2000000 + i
            e2e_t0 = time.perf_counter_ns()
            response, record = _execute_query(case, context, sockets, calibration, request_id, position, record=True)
            verify_t0 = time.perf_counter_ns()
            block = accept_response(context, response, case)
            verify_t1 = time.perf_counter_ns()
            e2e_t1 = time.perf_counter_ns()
            if block is not None:
                accepted += 1
            record.update({'request_id': request_id, 'position': position, 'local_verify_ns': verify_t1 - verify_t0, 'end_to_end_ns': e2e_t1 - e2e_t0, 'accepted': block is not None})
            raw_queries.append(record)
            last_response = response
        cpu_t1 = time.process_time_ns()
        batch_t1 = time.perf_counter_ns()
        if accepted != measured:
            raise RuntimeError(f'valid measured responses accepted {accepted}/{measured}')
        if last_response is None:
            raise RuntimeError('no response available for operation audit')
        counter = OperationCounter()
        if accept_response(context, last_response, case, counter) is None:
            raise RuntimeError('operation-audit response was rejected')
        expected = _expected_operations(case)
        checks = {'all_measured_queries_accepted': accepted == measured, 'operation_hash_count_exact': counter.hash_calls == expected['hash_calls'], 'signature_count_exact': counter.signature_verifications == expected['signature_verifications'], 'profile_has_positive_bandwidth': profile.bandwidth_mbps > 0, 'all_raw_queries_recorded': len(raw_queries) == measured, 'all_service_metric_files_declared': set(server_metric_files) == set(roles)}
        if not all(checks.values()):
            raise RuntimeError(f'internal E3 audit failed: {[k for k, v in checks.items() if not v]}')
        e2e = [int(q['end_to_end_ns']) for q in raw_queries]
        local = [int(q['local_verify_ns']) for q in raw_queries]
        result = {'schema_version': 1, 'experiment': 'E3', 'kind': 'network_trial', 'case': case, 'variant': case_variant(case), 'system': system, 'verifier_fixture': context.setup_metadata, 'endpoints': endpoints, 'server_metric_files': server_metric_files, 'calibration': calibration, 'profile': {'profile_id': profile.profile_id, 'name': profile.name, 'rtt_ms': profile.rtt_ms, 'bandwidth_mbps': profile.bandwidth_mbps, 'loss_rate': profile.loss_rate, 'mss_bytes': profile.mss_bytes}, 'timing': {'end_to_end_ns': summarise(e2e), 'local_verify_ns': summarise(local), 'batch_wall_ns': batch_t1 - batch_t0, 'batch_cpu_ns': cpu_t1 - cpu_t0, 'queries_per_second': measured * 1000000000.0 / (batch_t1 - batch_t0), 'raw_queries': raw_queries}, 'operations': {'observed': {'hash_calls_per_query': counter.hash_calls, 'signature_verifications_per_query': counter.signature_verifications, 'hash_calls_by_phase': counter.by_phase_hash_calls}, 'expected': expected}, 'resources': {'rss_after_fixture_kib': context.setup_metadata.get('rss_after_kib'), 'rss_after_benchmark_kib': get_rss_kib(), 'peak_rss_kib': get_peak_rss_kib()}, 'checks': checks}
        return result
    finally:
        for sock in sockets.values():
            try:
                sock.close()
            except OSError:
                pass
        try:
            context.anchor_backend.close()
        except Exception:
            pass

def _execute_query(case: dict[str, Any], context: Any, sockets: dict[str, socket.socket], calibration: dict[str, Any], request_id: int, position: int, *, record: bool):
    q = (context.shard, context.epoch, position)
    deployment = str(case.get('deployment', 'coalesced'))
    if deployment == 'coalesced':
        frame, txn = _one_transaction(sockets['gateway'], case=case, role='gateway', request_type=MSG_QUERY_COALESCED, request_id=request_id, q=q, baseline_rtt_ns=int(calibration['gateway']['median_ns']), transaction_index=0)
        decode_t0 = time.perf_counter_ns()
        decoded_id, response, _meta = decode_coalesced_response(frame)
        decode_t1 = time.perf_counter_ns()
        if decoded_id != request_id:
            raise RuntimeError('coalesced response request ID mismatch')
        result = {'transactions': [txn], 'response_deserialize_ns': decode_t1 - decode_t0, 'transaction_count': 1, 'total_request_bytes': txn['request_frame_bytes'], 'total_response_bytes': txn['response_frame_bytes']}
        return (response, result)
    payload_frame, payload_txn = _one_transaction(sockets['payload'], case=case, role='payload', request_type=MSG_QUERY_PAYLOAD, request_id=request_id, q=q, baseline_rtt_ns=int(calibration['payload']['median_ns']), transaction_index=0)
    payload_decode_t0 = time.perf_counter_ns()
    payload_part = decode_payload_response(payload_frame)
    payload_decode_t1 = time.perf_counter_ns()
    witness_frame, witness_txn = _one_transaction(sockets['witness'], case=case, role='witness', request_type=MSG_QUERY_WITNESS, request_id=request_id, q=q, baseline_rtt_ns=int(calibration['witness']['median_ns']), transaction_index=1)
    witness_decode_t0 = time.perf_counter_ns()
    witness_part = decode_witness_response(witness_frame)
    decoded_id, response, _meta = merge_split_response(payload_part, witness_part)
    witness_decode_t1 = time.perf_counter_ns()
    if decoded_id != request_id:
        raise RuntimeError('split response request ID mismatch')
    result = {'transactions': [payload_txn, witness_txn], 'response_deserialize_ns': payload_decode_t1 - payload_decode_t0 + (witness_decode_t1 - witness_decode_t0), 'transaction_count': 2, 'total_request_bytes': payload_txn['request_frame_bytes'] + witness_txn['request_frame_bytes'], 'total_response_bytes': payload_txn['response_frame_bytes'] + witness_txn['response_frame_bytes']}
    return (response, result)

def write_case_result(case: dict[str, Any], output_dir: Path, *, endpoints: dict[str, dict[str, Any]] | None=None, server_metric_files: dict[str, str] | None=None) -> Path:
    if case['kind'] == 'wire_audit':
        result = _wire_audit(case)
    else:
        if endpoints is None or server_metric_files is None:
            raise ValueError('network_trial requires endpoints and server metric files')
        result = run_network_case(case, endpoints, server_metric_files)
    path = output_dir / result_filename(case)
    atomic_write_json(path, result, compress=True)
    return path

def _wait_ready(path: Path, process: subprocess.Popen[Any], timeout_s: float=180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return read_json(path)
        rc = process.poll()
        if rc is not None:
            raise RuntimeError(f'service exited before readiness with status {rc}')
        time.sleep(0.05)
    raise TimeoutError(f'service readiness timed out: {path}')

def _valid_result(path: Path, case_id: str) -> bool:
    try:
        obj = read_json(path)
    except Exception:
        return False
    return obj.get('schema_version') == 1 and obj.get('experiment') == 'E3' and (obj.get('case', {}).get('case_id') == case_id) and isinstance(obj.get('checks'), dict) and all(obj['checks'].values())

def run_block_local(block: dict[str, Any], *, output_dir: Path, launcher: Path, python_executable: str | None=None) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    services_dir = output_dir / 'services'
    services_dir.mkdir(parents=True, exist_ok=True)
    cases = list(block['cases'])
    random.Random(int(block['case_order_seed'])).shuffle(cases)
    written: list[Path] = []
    py = python_executable or sys.executable
    for case in cases:
        target = output_dir / result_filename(case)
        if target.exists() and _valid_result(target, str(case['case_id'])):
            written.append(target)
            continue
        if case['kind'] == 'wire_audit':
            written.append(write_case_result(case, output_dir))
            continue
        expected = int(case['warmup_queries']) + int(case['measured_queries'])
        roles = ['gateway'] if case.get('deployment') == 'coalesced' else ['payload', 'witness']
        processes: dict[str, subprocess.Popen[Any]] = {}
        endpoints: dict[str, dict[str, Any]] = {}
        metric_files: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix='e3-case-', dir=os.environ.get('SLURM_TMPDIR')) as tmp:
            tmpdir = Path(tmp)
            case_file = tmpdir / 'case.json'
            case_file.write_text(json.dumps(case, sort_keys=True), encoding='utf-8')
            try:
                for role in roles:
                    ready = tmpdir / f'{role}.ready.json'
                    metrics = services_dir / server_metrics_filename(case, role)
                    metric_files[role] = str(metrics)
                    cmd = [py, str(launcher), 'serve', '--case-file', str(case_file), '--role', role, '--host', '127.0.0.1', '--port', '0', '--ready-file', str(ready), '--metrics-file', str(metrics), '--expected-queries', str(expected)]
                    processes[role] = subprocess.Popen(cmd)
                    endpoints[role] = _wait_ready(ready, processes[role])
                written.append(write_case_result(case, output_dir, endpoints=endpoints, server_metric_files=metric_files))
                for role, process in processes.items():
                    rc = process.wait(timeout=float(case.get('socket_timeout_s', 600.0)) + 60.0)
                    if rc != 0:
                        raise RuntimeError(f'{role} service exited with status {rc}')
            finally:
                for process in processes.values():
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
    return written
