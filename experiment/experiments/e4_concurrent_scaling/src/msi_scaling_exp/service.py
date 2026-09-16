from __future__ import annotations
import json, os, random, socket, socketserver, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .crypto import Ed25519Signer, deterministic_seed
from .index import DatasetSpec, hot_indices, index_to_key, key_to_index, refs_for
from .merkle import canonical_payload, fixture_root, hot_fixture, path_fixture, root_statement
from .util import atomic_write_json, precise_wait_ns, summarise
from .wire import *

def _proc_status():
    o = {'rss_kib': None, 'hwm_kib': None, 'voluntary_ctxt': None, 'nonvoluntary_ctxt': None}
    try:
        t = Path(f'/proc/{os.getpid()}/status').read_text(errors='replace')
    except OSError:
        return o
    for l in t.splitlines():
        if l.startswith('VmRSS:'):
            o['rss_kib'] = int(l.split()[1])
        elif l.startswith('VmHWM:'):
            o['hwm_kib'] = int(l.split()[1])
        elif l.startswith('voluntary_ctxt_switches:'):
            o['voluntary_ctxt'] = int(l.split()[1])
        elif l.startswith('nonvoluntary_ctxt_switches:'):
            o['nonvoluntary_ctxt'] = int(l.split()[1])
    return o

@dataclass(frozen=True)
class ServiceConfig:
    spec: DatasetSpec
    bandwidth_mbps_per_connection: float = 100.0
    service_cpus: int = 8
    key_seed: int = 44022026
    socket_timeout_s: float = 60.0

class ServiceStats:

    def __init__(self, cpus: int):
        self.lock = threading.Lock()
        self.cpus = max(1, cpus)
        self.rng = random.Random(44042026)
        self._reset()

    def _reset(self):
        self.wall = time.perf_counter_ns()
        self.cpu = time.process_time_ns()
        self.requests = self.queries = self.pings = self.control = self.errors = self.unavailable = self.rx = self.tx = self.processing = self.sign = self.serialize = self.bw_wait = self.active = self.max_active = self.seen = 0
        self.proc_samples = []
        self.byte_samples = []

    def begin(self, rx):
        with self.lock:
            self.requests += 1
            self.rx += rx
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _sample(self, p, b):
        self.seen += 1
        cap = 4096
        if len(self.proc_samples) < cap:
            self.proc_samples.append(p)
            self.byte_samples.append(b)
        else:
            j = self.rng.randrange(self.seen)
            if j < cap:
                self.proc_samples[j] = p
                self.byte_samples[j] = b

    def finish_query(self, tx, proc, sign, ser, bw, unavailable=False):
        with self.lock:
            self.queries += 1
            self.tx += tx
            self.processing += proc
            self.sign += sign
            self.serialize += ser
            self.bw_wait += bw
            self.unavailable += int(unavailable)
            self._sample(proc, tx)
            self.active = max(0, self.active - 1)

    def finish_control(self, tx, kind):
        with self.lock:
            self.control += 1
            self.pings += int(kind == KIND_PING)
            self.tx += tx
            self.active = max(0, self.active - 1)

    def finish_error(self):
        with self.lock:
            self.errors += 1
            self.active = max(0, self.active - 1)

    def snapshot(self):
        with self.lock:
            wall = max(1, time.perf_counter_ns() - self.wall)
            cpu = max(0, time.process_time_ns() - self.cpu)
            return {'wall_ns': wall, 'process_cpu_ns': cpu, 'cpu_utilization_pct_of_allocated': 100 * cpu / (wall * self.cpus), 'requests': self.requests, 'queries': self.queries, 'pings': self.pings, 'control': self.control, 'errors': self.errors, 'unavailable': self.unavailable, 'bytes_rx': self.rx, 'bytes_tx': self.tx, 'processing_ns': self.processing, 'sign_ns': self.sign, 'serialize_ns': self.serialize, 'bandwidth_wait_ns': self.bw_wait, 'active': self.active, 'max_active': self.max_active, 'processing_latency_ns': summarise(self.proc_samples), 'response_frame_bytes': summarise(self.byte_samples), **_proc_status()}

    def reset(self):
        with self.lock:
            self._reset()

class ServiceState:

    def __init__(self, cfg: ServiceConfig):
        self.config = cfg
        self.spec = cfg.spec
        self.hot = set(hot_indices(self.spec))
        self.key_seed = deterministic_seed('anchor', cfg.key_seed)
        s = Ed25519Signer(self.key_seed)
        self.public_key = s.public_key
        s.close()
        self.stats = ServiceStats(cfg.service_cpus)
        self.shutdown_event = threading.Event()
        self.hot_cache = {}
        for idx in self.hot:
            sh, ep = index_to_key(self.spec, idx)
            self.hot_cache[idx] = hot_fixture(sh, ep, self.spec.epoch_length, self.spec.block_bytes, self.spec.seed)

    def fixture(self, sh, ep, mode):
        idx = key_to_index(self.spec, sh, ep)
        p_ref, a_ref, _ = refs_for(self.spec, sh, ep)
        if idx in self.hot_cache:
            payload, path, leaves, root = self.hot_cache[idx]
            w = encode_leaf_vector(leaves) if mode == MODE_LEAF else encode_path(path)
            return (payload, w, root, root_statement(sh, ep, self.spec.epoch_length, root), p_ref, a_ref)
        if mode == MODE_LEAF:
            raise LookupError('leaf mode is restricted to deterministic hot-set records')
        payload = canonical_payload(sh, ep, self.spec.block_bytes, self.spec.seed, 0)
        path = path_fixture(sh, ep, self.spec.epoch_length.bit_length() - 1, self.spec.seed)
        root = fixture_root(sh, ep, self.spec.epoch_length, self.spec.block_bytes, self.spec.seed)
        return (payload, encode_path(path), root, root_statement(sh, ep, self.spec.epoch_length, root), p_ref, a_ref)

class Handler(socketserver.BaseRequestHandler):
    server: 'Server'

    def setup(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.request.settimeout(self.server.state.config.socket_timeout_s)
        self.signer = Ed25519Signer(self.server.state.key_seed)

    def finish(self):
        self.signer.close()

    def handle(self):
        while not self.server.state.shutdown_event.is_set():
            try:
                frame = recv_frame(self.request)
            except (EOFError, OSError, ValueError):
                return
            self.server.state.stats.begin(len(frame) + 4)
            try:
                req = decode_request(frame)
                if req.kind == KIND_QUERY:
                    self.query(req)
                elif req.kind == KIND_PING:
                    self.control(req, {'pong': req.query_id, 'server_perf_ns': time.perf_counter_ns()})
                elif req.kind == KIND_STATS:
                    self.control(req, self.server.state.stats.snapshot())
                elif req.kind == KIND_RESET:
                    before = self.server.state.stats.snapshot()
                    self.control(req, {'reset': True, 'previous': before})
                    self.server.state.stats.reset()
                elif req.kind == KIND_SHUTDOWN:
                    self.control(req, {'shutdown': True})
                    self.server.state.shutdown_event.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                else:
                    self.error(req, STATUS_BAD_REQUEST, 'unknown request kind')
            except Exception as e:
                self.server.state.stats.finish_error()
                try:
                    self.error(Request(KIND_QUERY, MODE_NONE, getattr(locals().get('req', None), 'query_id', 0)), STATUS_INTERNAL_ERROR, f'{type(e).__name__}: {e}', already_finished=True)
                except Exception:
                    return

    def query(self, req):
        st = self.server.state
        start = time.perf_counter_ns()
        if req.mode not in (MODE_FULL, MODE_LEAF, MODE_EXT) or not 0 <= req.shard < st.spec.shard_count or (not 0 <= req.epoch < st.spec.historical_epochs):
            self.error(req, STATUS_BAD_REQUEST, 'invalid query')
            return
        try:
            payload, w, root, stmt, p_ref, a_ref = st.fixture(req.shard, req.epoch, req.mode)
        except LookupError as e:
            enc = encode_response(control_response(KIND_QUERY, req.query_id, {'error': str(e)}, STATUS_UNAVAILABLE))
            sent = send_frame(self.request, enc)
            st.stats.finish_query(sent, time.perf_counter_ns() - start, 0, 0, 0, True)
            return
        s0 = time.perf_counter_ns()
        sig = self.signer.sign(stmt)
        sign_ns = time.perf_counter_ns() - s0
        r = Response(KIND_QUERY, STATUS_OK, req.mode, req.query_id, req.shard, req.epoch, st.spec.epoch_length, p_ref, a_ref, root, payload, w, sig)
        z = time.perf_counter_ns()
        enc = encode_response(r)
        ser = time.perf_counter_ns() - z
        proc = time.perf_counter_ns() - start
        delay = int((len(enc) + 4) * 8 / (st.config.bandwidth_mbps_per_connection * 1000000.0) * 1000000000.0)
        obs = precise_wait_ns(delay)
        sent = send_frame(self.request, enc)
        st.stats.finish_query(sent, proc, sign_ns, ser, obs)

    def control(self, req, obj):
        enc = encode_response(control_response(req.kind, req.query_id, obj))
        sent = send_frame(self.request, enc)
        self.server.state.stats.finish_control(sent, req.kind)

    def error(self, req, status, msg, already_finished=False):
        enc = encode_response(control_response(req.kind, req.query_id, {'error': msg}, status))
        sent = send_frame(self.request, enc)
        if not already_finished:
            self.server.state.stats.finish_control(sent, req.kind)

class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)

def serve(config: ServiceConfig, bind_host='0.0.0.0', port=0, ready_file=None, final_stats_file=None, advertise_host=None):
    state = ServiceState(config)
    server = Server((bind_host, port), state)
    host, p = server.server_address
    ready = {'schema_version': 1, 'host': advertise_host or socket.getfqdn() or socket.gethostname(), 'bind_host': host, 'port': p, 'pid': os.getpid(), 'public_key_hex': state.public_key.hex(), 'dataset_id': config.spec.dataset_id, 'spec': config.spec.as_dict(), 'bandwidth_mbps_per_connection': config.bandwidth_mbps_per_connection, 'service_cpus': config.service_cpus, 'started_unix_ns': time.time_ns()}
    if ready_file:
        atomic_write_json(ready_file, ready)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        final = {**ready, 'finished_unix_ns': time.time_ns(), 'stats': state.stats.snapshot()}
        if final_stats_file:
            atomic_write_json(final_stats_file, final)
        server.server_close()
    return ready
