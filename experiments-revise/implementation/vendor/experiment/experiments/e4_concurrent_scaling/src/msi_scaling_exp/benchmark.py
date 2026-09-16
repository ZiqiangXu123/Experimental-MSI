from __future__ import annotations
import csv, gzip, json, math, multiprocessing as mp, os, queue, socket, statistics, tempfile, time, traceback
from pathlib import Path
from typing import Any
from .crypto import Ed25519Verifier
from .index import DatasetSpec, IndexReader, dataset_paths
from .merkle import check_canonical_payload, path_from_leaves, root_statement, verify_member
from .monitor import RuntimeSampler, delta_snapshot, process_snapshot, system_snapshot
from .service import ServiceConfig, serve
from .util import atomic_write_json_gz, precise_wait_ns, read_json, summarise, system_metadata
from .wire import *
from .workload import QuerySampler
RESULT_FIELDS = ('qid', 'step', 'shard', 'epoch', 'client', 'accepted', 'error', 'scheduled_ns', 'submit_ns', 'worker_start_ns', 'done_ns', 'lookup_ns', 'fetch_ns', 'check_ns', 'decode_ns', 'resolve_ns', 'member_ns', 'anchor_ns', 'response_bytes', 'hash_calls', 'worker')

def _connect(host: str, port: int, timeout: float=30.0):
    s = socket.create_connection((host, port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(timeout)
    return s

def control(host, port, kind, qid=1):
    with _connect(host, port) as s:
        send_frame(s, encode_request(Request(kind, MODE_NONE, qid)))
        r = decode_response(recv_frame(s))
        return decode_control_json(r)

def _worker(worker_id: int, case: dict[str, Any], index_path: str, host: str, port: int, public_key: bytes, task_q: Any, result_q: Any, ready_q: Any):
    reader = None
    sock = None
    verifier = None
    try:
        try:
            allowed = sorted(os.sched_getaffinity(0))
            if case.get('pin_workers', True) and allowed:
                os.sched_setaffinity(0, {allowed[worker_id % len(allowed)]})
        except Exception:
            pass
        reader = IndexReader(index_path)
        verifier = Ed25519Verifier(public_key)
        sock = _connect(host, port, 60)
        rtts = []
        for i in range(int(case.get('rtt_probe_count', 7))):
            qid = (worker_id + 1) * 10000000 + i
            st = time.perf_counter_ns()
            send_frame(sock, encode_request(Request(KIND_PING, MODE_NONE, qid)))
            r = decode_response(recv_frame(sock))
            if r.status != STATUS_OK:
                raise RuntimeError('ping failed')
            rtts.append(time.perf_counter_ns() - st)
        physical = int(statistics.median(rtts))
        target = int(float(case['target_rtt_ms']) * 1000000.0)
        inject = max(0, target - physical)
        ready_q.put({'worker': worker_id, 'pid': os.getpid(), 'physical_rtt_ns': physical, 'injected_rtt_ns': inject})
        mode = MODE_NAME_TO_CODE[case['mode']]
        depth = int(case['epoch_length']).bit_length() - 1
        expected_witness = int(case['epoch_length']) * 32 if mode == MODE_LEAF else depth * 33
        while True:
            task = task_q.get()
            if task is None:
                break
            qid, scheduled, submitted, shard, epoch, client, step = task
            ws = time.perf_counter_ns()
            accepted = False
            error = ''
            lookup = fetch = check = decode = resolve = member = anchor = 0
            resp_bytes = 0
            hash_calls = 0
            try:
                z = time.perf_counter_ns()
                rec, lookup = reader.lookup(shard, epoch)
                half = inject // 2
                f0 = time.perf_counter_ns()
                precise_wait_ns(half)
                send_frame(sock, encode_request(Request(KIND_QUERY, mode, qid, shard, epoch)))
                raw = recv_frame(sock)
                resp_bytes = len(raw) + 4
                r = decode_response(raw)
                precise_wait_ns(inject - half)
                fetch = time.perf_counter_ns() - f0
                c0 = time.perf_counter_ns()
                ok = r.status == STATUS_OK and r.kind == KIND_QUERY and (r.mode == mode) and (r.query_id == qid) and (r.shard == shard) and (r.epoch == epoch) and (r.n == rec['n']) and (r.payload_ref == rec['payload_ref']) and (r.aux_ref == rec['aux_ref']) and (r.root == rec['root']) and (len(r.payload) == int(case['block_bytes'])) and (len(r.witness) == expected_witness) and (len(r.signature) == 64)
                if mode == MODE_LEAF:
                    ok = ok and rec['hot']
                check = time.perf_counter_ns() - c0
                if not ok:
                    raise ValueError('CheckResp rejected response')
                d0 = time.perf_counter_ns()
                if not check_canonical_payload(r.payload, shard, epoch, int(case['block_bytes']), int(case['fixture_seed']), 0):
                    raise ValueError('DecodePayload failed')
                decode = time.perf_counter_ns() - d0
                x0 = time.perf_counter_ns()
                if mode == MODE_LEAF:
                    leaves = decode_leaf_vector(r.witness, int(case['epoch_length']))
                    path = path_from_leaves(0, leaves)
                    hash_calls += int(case['epoch_length']) - 1
                else:
                    path = decode_path(r.witness, depth)
                resolve = time.perf_counter_ns() - x0
                m0 = time.perf_counter_ns()
                accepted = verify_member(shard, epoch, 0, r.payload, path, rec['root'])
                member = time.perf_counter_ns() - m0
                hash_calls += depth + 1
                if not accepted:
                    raise ValueError('VerifyMember failed')
                a0 = time.perf_counter_ns()
                stmt = root_statement(shard, epoch, int(case['epoch_length']), rec['root'])
                accepted = verifier.verify(stmt, r.signature)
                anchor = time.perf_counter_ns() - a0
                if not accepted:
                    raise ValueError('VerifyAnchor failed')
            except Exception as e:
                error = f'{type(e).__name__}: {e}'
                accepted = False
            done = time.perf_counter_ns()
            result_q.put((qid, step, shard, epoch, client, int(accepted), error, scheduled, submitted, ws, done, lookup, fetch, check, decode, resolve, member, anchor, resp_bytes, hash_calls, worker_id))
    except Exception as e:
        ready_q.put({'worker': worker_id, 'pid': os.getpid(), 'fatal': f'{type(e).__name__}: {e}', 'traceback': traceback.format_exc()})
    finally:
        try:
            sock.close() if sock else None
        except Exception:
            pass
        try:
            reader.close() if reader else None
        except Exception:
            pass
        try:
            verifier.close() if verifier else None
        except Exception:
            pass

def _service_process(cfg_dict, ready_file, stats_file):
    spec = DatasetSpec.from_dict(cfg_dict['spec'])
    cfg = ServiceConfig(spec, float(cfg_dict['bandwidth_mbps']), int(cfg_dict['service_cpus']), int(cfg_dict['key_seed']))
    serve(cfg, '127.0.0.1', 0, ready_file, stats_file, '127.0.0.1')

def _wait_json(path: Path, timeout=60):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if path.exists():
                return read_json(path)
        except Exception as e:
            last = e
        time.sleep(0.05)
    raise TimeoutError(f'ready file {path} not available: {last}')

def _row_dict(t):
    return dict(zip(RESULT_FIELDS, t))

def _drain(q, outstanding, handler, timeout=30):
    end = time.time() + timeout
    while outstanding > 0:
        if time.time() > end:
            raise TimeoutError(f'{outstanding} queries did not drain')
        try:
            r = q.get(timeout=0.1)
        except queue.Empty:
            continue
        handler(r)
        outstanding -= 1
    return outstanding

def _closed_loop(case, task_q, result_q, sampler, qid_ref, duration_s):
    c = int(case['concurrent_clients'])
    start = time.perf_counter_ns()
    end = start + int(duration_s * 1000000000.0)
    outstanding = 0
    completed = accepted = 0

    def submit(now):
        qid_ref[0] += 1
        sh, ep = sampler.sample((now - start) / 1000000000.0)
        task_q.put((qid_ref[0], now, now, sh, ep, qid_ref[0] % c, -1))
        return 1
    for _ in range(c):
        outstanding += submit(time.perf_counter_ns())
    while time.perf_counter_ns() < end:
        try:
            r = result_q.get(timeout=0.05)
        except queue.Empty:
            continue
        outstanding -= 1
        d = _row_dict(r)
        completed += 1
        accepted += d['accepted']
        now = time.perf_counter_ns()
        if now < end:
            outstanding += submit(now)
    _drain(result_q, outstanding, lambda r: None, 60)
    return {'duration_s': duration_s, 'completed_within_window': completed, 'accepted_within_window': accepted, 'capacity_qps': accepted / duration_s if duration_s else 0}

def _open_loop(case, task_q, result_q, sampler, qid_ref, duration_s, target_rate, step_index, writer=None, record=False):
    c = int(case['concurrent_clients'])
    start = time.perf_counter_ns()
    end = start + int(duration_s * 1000000000.0)
    interval = max(1, int(1000000000.0 / max(0.001, target_rate)))
    next_due = start
    outstanding = 0
    offered = submitted = backpressure = 0
    accepted_total = accepted_window = failed = 0
    last_done = start
    phases = {k: [] for k in ('e2e_ns', 'scheduled_e2e_ns', 'queue_ns', 'lookup_ns', 'fetch_ns', 'check_ns', 'decode_ns', 'resolve_ns', 'member_ns', 'anchor_ns')}
    hashes = []
    bytes_list = []
    offered_shard = {}
    submitted_shard = {}
    accepted_shard = {}
    lat_shard = {}

    def handle(t):
        nonlocal outstanding, accepted_total, accepted_window, failed, last_done
        d = _row_dict(t)
        outstanding -= 1
        last_done = max(last_done, d['done_ns'])
        e2e = d['done_ns'] - d['submit_ns']
        sched = d['done_ns'] - d['scheduled_ns']
        qwait = d['worker_start_ns'] - d['submit_ns']
        d.update({'e2e_ns': e2e, 'scheduled_e2e_ns': sched, 'queue_ns': qwait, 'target_rate_qps': target_rate})
        if d['accepted']:
            accepted_total += 1
            accepted_shard[d['shard']] = accepted_shard.get(d['shard'], 0) + 1
            lat_shard.setdefault(d['shard'], []).append(e2e)
            if d['done_ns'] <= end:
                accepted_window += 1
        else:
            failed += 1
        for k in phases:
            phases[k].append(d[k])
        hashes.append(d['hash_calls'])
        bytes_list.append(d['response_bytes'])
        if record and writer:
            writer.writerow(d)
    while True:
        now = time.perf_counter_ns()
        while now < end and now >= next_due:
            scheduled = next_due
            next_due += interval
            offered += 1
            sh, ep = sampler.sample((scheduled - start) / 1000000000.0)
            offered_shard[sh] = offered_shard.get(sh, 0) + 1
            if outstanding < c:
                qid_ref[0] += 1
                try:
                    task_q.put_nowait((qid_ref[0], scheduled, now, sh, ep, qid_ref[0] % c, step_index))
                    outstanding += 1
                    submitted += 1
                    submitted_shard[sh] = submitted_shard.get(sh, 0) + 1
                except queue.Full:
                    backpressure += 1
            else:
                backpressure += 1
            now = time.perf_counter_ns()
        drained = False
        while True:
            try:
                r = result_q.get_nowait()
                handle(r)
                drained = True
            except queue.Empty:
                break
        if now >= end and outstanding == 0:
            break
        if not drained:
            timeout = 0.001
            if now < end:
                timeout = min(timeout, max(0, (next_due - now) / 1000000000.0))
            try:
                handle(result_q.get(timeout=timeout))
            except queue.Empty:
                pass
    wall = max(duration_s, (last_done - start) / 1000000000.0)
    service_ratios = []
    per_shard = []
    for sh, count in sorted(offered_shard.items()):
        sub = submitted_shard.get(sh, 0)
        acc = accepted_shard.get(sh, 0)
        ratio = acc / sub if sub else 1.0
        service_ratios.append(ratio)
        ls = lat_shard.get(sh, [])
        per_shard.append({'shard': sh, 'offered': count, 'submitted': sub, 'accepted': acc, 'service_ratio': ratio, 'p95_e2e_ns': summarise(ls)['p95']})
    from .util import jains_fairness
    return {'step_index': step_index, 'duration_s': duration_s, 'target_rate_qps': target_rate, 'offered_attempts': offered, 'submitted': submitted, 'backpressure': backpressure, 'accepted_total': accepted_total, 'accepted_within_window': accepted_window, 'failed': failed, 'throughput_qps': accepted_window / duration_s if duration_s else 0, 'drain_throughput_qps': accepted_total / wall if wall else 0, 'response_bytes': summarise(bytes_list), 'hash_calls': summarise(hashes), 'latency': {k: summarise(v) for k, v in phases.items()}, 'per_shard': per_shard, 'jain_service_ratio': jains_fairness(service_ratios), 'starved_shards': sum((1 for x in per_shard if x['submitted'] >= 10 and x['accepted'] == 0))}

def run_case(case: dict[str, Any], dataset_dir: str | Path, raw_dir: str | Path, external_ready_file: str | Path | None=None):
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / 'queries').mkdir(exist_ok=True)
    spec = DatasetSpec.from_dict(case)
    index_path, _ = dataset_paths(dataset_dir, spec)
    if not index_path.exists():
        raise FileNotFoundError(f'prepared index not found: {index_path}')
    service_proc = None
    task_q = result_q = ready_q = None
    workers: list[Any] = []
    host: str | None = None
    port: int | None = None
    tmp_ready: Path | None = None
    query_path = raw_dir / 'queries' / f"{case['case_id']}.csv.gz"
    tmp_query = query_path.with_suffix(query_path.suffix + f'.tmp.{os.getpid()}')
    final_stats = raw_dir / f"service-{case['case_id']}.json"
    ready: dict[str, Any] = {}
    ready_workers: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    calibration: dict[str, Any] = {}
    start_method = os.environ.get('E4_MP_START_METHOD')
    if not start_method:
        available = mp.get_all_start_methods()
        start_method = 'forkserver' if 'forkserver' in available else 'fork' if 'fork' in available else 'spawn'
    ctx = mp.get_context(start_method)
    try:
        if external_ready_file:
            ready = _wait_json(Path(external_ready_file), 120)
        else:
            tmp_ready = raw_dir / f"service-ready-{case['case_id']}.json"
            for stale in (tmp_ready, final_stats):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
            config = {'spec': spec.as_dict(), 'bandwidth_mbps': case['bandwidth_mbps'], 'service_cpus': case['service_cpus'], 'key_seed': case['anchor_key_seed']}
            service_proc = ctx.Process(target=_service_process, args=(config, str(tmp_ready), str(final_stats)), daemon=False)
            service_proc.start()
            ready = _wait_json(tmp_ready, 60)
        host = str(ready['host'])
        port = int(ready['port'])
        public_key = bytes.fromhex(ready['public_key_hex'])
        task_q = ctx.Queue(maxsize=max(64, int(case['concurrent_clients']) * 2))
        result_q = ctx.Queue(maxsize=max(128, int(case['concurrent_clients']) * 4))
        ready_q = ctx.Queue()
        for worker_id in range(int(case['verifier_workers'])):
            process = ctx.Process(target=_worker, args=(worker_id, case, str(index_path), host, port, public_key, task_q, result_q, ready_q))
            process.start()
            workers.append(process)
        for _ in workers:
            message = ready_q.get(timeout=60)
            if 'fatal' in message:
                raise RuntimeError(message['fatal'] + '\n' + message.get('traceback', ''))
            ready_workers.append(message)
        query_id = [int(case['trial']) * 1000000000]
        sampler = QuerySampler(spec, case['distribution'], int(case['seed']), case['mode'] == 'leaf', float(case.get('burst_period_s', 1.0)))
        calibration = _closed_loop(case, task_q, result_q, sampler, query_id, float(case['calibration_s']))
        capacity = max(1.0, float(calibration['capacity_qps']))
        multipliers = case['rate_multipliers'] if case['trial_kind'] == 'ramp' else [case['steady_multiplier']]
        targets = [min(float(case['max_offered_qps']), max(1.0, capacity * float(multiplier))) for multiplier in multipliers]
        fields = list(RESULT_FIELDS) + ['e2e_ns', 'scheduled_e2e_ns', 'queue_ns', 'target_rate_qps']
        all_pids = [os.getpid()] + [process.pid for process in workers]
        with gzip.open(tmp_query, 'wt', encoding='utf-8', newline='', compresslevel=5) as query_file:
            writer = csv.DictWriter(query_file, fieldnames=fields)
            writer.writeheader()
            for step_index, target in enumerate(targets):
                warm_sampler = QuerySampler(spec, case['distribution'], int(case['seed']) + 10000 + step_index, case['mode'] == 'leaf', float(case.get('burst_period_s', 1.0)))
                _open_loop(case, task_q, result_q, warm_sampler, query_id, float(case['warmup_s']), target, -100 - step_index, None, False)
                measure_sampler = QuerySampler(spec, case['distribution'], int(case['seed']) + 20000 + step_index, case['mode'] == 'leaf', float(case.get('burst_period_s', 1.0)))
                try:
                    control(host, port, KIND_RESET, 900000000 + step_index)
                except Exception:
                    pass
                before_process = process_snapshot(all_pids)
                before_system = system_snapshot()
                runtime_sampler = RuntimeSampler(all_pids, float(case.get('sample_interval_s', 0.1)))
                runtime_sampler.start()
                started = time.perf_counter_ns()
                summary = _open_loop(case, task_q, result_q, measure_sampler, query_id, float(case['measure_s']), target, step_index, writer, True)
                wall_ns = time.perf_counter_ns() - started
                runtime = runtime_sampler.stop()
                after_process = process_snapshot(all_pids)
                after_system = system_snapshot()
                resource = delta_snapshot(before_process, after_process, wall_ns, int(case['verifier_workers']))
                resource.update({'system_context_switches': None if before_system['ctxt'] is None else int(after_system['ctxt']) - int(before_system['ctxt']), 'network_rx_bytes': None if before_system['net_rx_bytes'] is None else int(after_system['net_rx_bytes']) - int(before_system['net_rx_bytes']), 'network_tx_bytes': None if before_system['net_tx_bytes'] is None else int(after_system['net_tx_bytes']) - int(before_system['net_tx_bytes']), **runtime})
                try:
                    service_stats = control(host, port, KIND_STATS, 910000000 + step_index)
                except Exception as exc:
                    service_stats = {'error': str(exc)}
                p95 = summary['latency']['e2e_ns']['p95'] or 0
                service_p95 = (service_stats.get('processing_latency_ns') or {}).get('p95') if isinstance(service_stats, dict) else None
                headroom = not service_stats.get('error') and int(service_stats.get('errors', 0)) == 0 and (float(service_stats.get('cpu_utilization_pct_of_allocated', 0)) < 85) and (service_p95 is None or p95 == 0 or float(service_p95) < 0.25 * float(p95))
                summary.update({'resource': resource, 'service': service_stats, 'service_headroom_pass': bool(headroom)})
                steps.append(summary)
        os.replace(tmp_query, query_path)
    finally:
        if task_q is not None:
            for _ in workers:
                try:
                    task_q.put(None, timeout=1)
                except Exception:
                    pass
        for process in workers:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        if host is not None and port is not None:
            try:
                control(host, port, KIND_SHUTDOWN, 999999999)
            except Exception:
                pass
        if service_proc is not None:
            service_proc.join(timeout=15)
            if service_proc.is_alive():
                service_proc.terminate()
                service_proc.join(timeout=5)
        for queue_object in (task_q, result_q, ready_q):
            if queue_object is None:
                continue
            try:
                queue_object.cancel_join_thread()
                queue_object.close()
            except Exception:
                pass
        try:
            tmp_query.unlink()
        except FileNotFoundError:
            pass
    expected_hash = int(case['epoch_length']) - 1 + int(case['epoch_length']).bit_length() if case['mode'] == 'leaf' else int(case['epoch_length']).bit_length()
    all_accepted = all((step['failed'] == 0 and step['accepted_total'] == step['submitted'] for step in steps))
    hash_ok = all((step['hash_calls']['min'] == expected_hash and step['hash_calls']['max'] == expected_hash for step in steps if step['hash_calls']['count']))
    headroom = all((step['service_headroom_pass'] for step in steps))
    result = {'schema_version': 1, 'experiment': 'E4', 'case': case, 'dataset': {'path': str(index_path), 'size_bytes': index_path.stat().st_size, 'total_records': spec.total_records, 'record_bytes': 64}, 'service_ready': ready, 'worker_calibration': ready_workers, 'capacity_calibration': calibration, 'rate_steps': steps, 'checks': {'all_queries_accepted': all_accepted, 'hash_calls_exact': hash_ok, 'expected_hash_calls_per_query': expected_hash, 'fixed_merkle_depth': int(case['epoch_length']).bit_length() - 1, 'service_headroom_all_steps': headroom, 'worker_exit_codes': [process.exitcode for process in workers]}, 'raw_query_file': str(query_path), 'system': system_metadata(), 'created_unix_ns': time.time_ns()}
    output = raw_dir / f"{case['case_id']}.json.gz"
    atomic_write_json_gz(output, result)
    return result
