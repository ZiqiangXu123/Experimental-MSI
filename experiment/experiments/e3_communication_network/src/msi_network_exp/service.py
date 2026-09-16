from __future__ import annotations
import os
import socket
import time
from pathlib import Path
from typing import Any
from .constants import MSG_CONFIG, MSG_PING, MSG_QUERY_COALESCED, MSG_QUERY_PAYLOAD, MSG_QUERY_WITNESS, SERVICE_ROLES
from .fixture import build_server_fixture
from .link import LinkProfile, apply_transfer_delay, sample_transfer
from .util import atomic_write_json, pin_to_cpu_offset, system_metadata
from .wire import decode_config, decode_ping, decode_query, encode_coalesced_response, encode_config_ack, encode_error, encode_payload_response, encode_pong, encode_witness_response, recv_frame, send_frame

def _allowed_query_type(role: str) -> int:
    if role == 'gateway':
        return MSG_QUERY_COALESCED
    if role == 'payload':
        return MSG_QUERY_PAYLOAD
    if role == 'witness':
        return MSG_QUERY_WITNESS
    raise ValueError(f'unknown service role {role}')

def _advertise_host(bound_host: str) -> str:
    override = os.environ.get('E3_ADVERTISE_HOST', '').strip()
    if override:
        return override
    if bound_host not in {'0.0.0.0', '::', ''}:
        return bound_host
    try:
        return socket.getfqdn()
    except OSError:
        return socket.gethostname()

def run_service(case: dict[str, Any], *, role: str, host: str, port: int, ready_file: Path, metrics_file: Path, expected_queries: int, accept_timeout_s: float=120.0, io_timeout_s: float=600.0) -> dict[str, Any]:
    if role not in SERVICE_ROLES:
        raise ValueError(f'role must be one of {SERVICE_ROLES}')
    if expected_queries < 1:
        raise ValueError('expected_queries must be positive')
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        ready_file.unlink()
    except FileNotFoundError:
        pass
    service_start = time.perf_counter_ns()
    role_cpu_offset = {'gateway': 1, 'payload': 1, 'witness': 2}[role]
    affinity = pin_to_cpu_offset(role_cpu_offset)
    fixture = build_server_fixture(case)
    profile = LinkProfile.from_id(str(case['profile']))
    allowed_type = _allowed_query_type(role)
    query_metrics: list[dict[str, Any]] = []
    error: str | None = None
    baseline_rtt_ns = 0
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, int(port)))
    listener.listen(1)
    listener.settimeout(accept_timeout_s)
    bound_host, bound_port = listener.getsockname()[:2]
    ready = {'schema_version': 1, 'experiment': 'E3', 'role': role, 'host': _advertise_host(str(bound_host)), 'bound_host': str(bound_host), 'port': int(bound_port), 'pid': os.getpid(), 'case_id': case['case_id']}
    atomic_write_json(ready_file, ready)
    conn: socket.socket | None = None
    try:
        conn, peer = listener.accept()
        conn.settimeout(io_timeout_s)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        completed = 0
        while completed < expected_queries:
            recv_t0 = time.perf_counter_ns()
            frame = recv_frame(conn)
            recv_t1 = time.perf_counter_ns()
            if frame.message_type == MSG_PING:
                request_id = decode_ping(frame.body)
                send_frame(conn, encode_pong(request_id))
                continue
            if frame.message_type == MSG_CONFIG:
                baseline_rtt_ns = decode_config(frame.body)
                send_frame(conn, encode_config_ack())
                continue
            if frame.message_type != allowed_type:
                send_frame(conn, encode_error(f'service role {role} rejects message type {frame.message_type}'))
                raise ValueError(f'unexpected message type {frame.message_type} for role {role}')
            decode_t0 = time.perf_counter_ns()
            request_id, q, scheme = decode_query(frame.body)
            decode_t1 = time.perf_counter_ns()
            if scheme != case['scheme']:
                send_frame(conn, encode_error('scheme mismatch'))
                raise ValueError('query scheme does not match service case')
            if q[0] != fixture.shard or q[1] != fixture.epoch:
                send_frame(conn, encode_error('query context mismatch'))
                raise ValueError('query context mismatch')
            lookup_t0 = time.perf_counter_ns()
            response = fixture.responses.get(q[2])
            lookup_t1 = time.perf_counter_ns()
            if response is None:
                send_frame(conn, encode_error('query position unavailable'))
                raise ValueError('query position not in deterministic pool')
            serialize_t0 = time.perf_counter_ns()
            if role == 'gateway':
                response_frame, breakdown = encode_coalesced_response(request_id, fixture.entry, response)
            elif role == 'payload':
                response_frame, breakdown = encode_payload_response(request_id, fixture.entry, response)
            else:
                response_frame, breakdown = encode_witness_response(request_id, fixture.entry, response)
            serialize_t1 = time.perf_counter_ns()
            link_sample = sample_transfer(profile, len(response_frame), baseline_rtt_ns=baseline_rtt_ns, seed=int(case['network_seed']), endpoint=role, direction='response', request_id=request_id, transaction_index=0)
            applied = apply_transfer_delay(link_sample)
            send_t0 = time.perf_counter_ns()
            send_frame(conn, response_frame)
            send_t1 = time.perf_counter_ns()
            query_metrics.append({'request_id': int(request_id), 'role': role, 'request_frame_bytes': frame.total_bytes, 'socket_receive_ns': recv_t1 - recv_t0, 'request_deserialize_ns': decode_t1 - decode_t0, 'service_lookup_ns': lookup_t1 - lookup_t0, 'response_serialize_ns': serialize_t1 - serialize_t0, 'response_socket_send_ns': send_t1 - send_t0, 'response_frame_bytes': len(response_frame), 'wire_breakdown': breakdown.as_dict(), 'response_link': applied.as_dict()})
            completed += 1
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        listener.close()
        result = {'schema_version': 1, 'experiment': 'E3', 'role': role, 'case': case, 'system': {**system_metadata(), 'affinity': affinity}, 'fixture': fixture.setup_metadata, 'ready': ready, 'physical_baseline_rtt_ns': baseline_rtt_ns, 'expected_queries': expected_queries, 'completed_queries': len(query_metrics), 'query_metrics': query_metrics, 'service_wall_ns': time.perf_counter_ns() - service_start, 'error': error, 'checks': {'all_expected_queries_served': len(query_metrics) == expected_queries, 'no_service_error': error is None, 'response_sizes_positive': all((m['response_frame_bytes'] > 0 for m in query_metrics))}}
        atomic_write_json(metrics_file, result, compress=True)
    return result
