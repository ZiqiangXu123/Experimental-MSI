from __future__ import annotations
import os, threading, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
CLK = os.sysconf('SC_CLK_TCK')

def _stat(pid):
    try:
        x = Path(f'/proc/{pid}/stat').read_text(errors='replace')
        r = x[x.rfind(')') + 2:].split()
        return {'minflt': int(r[7]), 'majflt': int(r[9]), 'ticks': int(r[11]) + int(r[12]), 'threads': int(r[17])}
    except Exception:
        return None

def _status(pid):
    try:
        t = Path(f'/proc/{pid}/status').read_text(errors='replace')
    except OSError:
        return None
    o = {'rss_kib': 0, 'hwm_kib': 0, 'voluntary_ctxt': 0, 'nonvoluntary_ctxt': 0}
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

def process_snapshot(pids: Iterable[int]):
    o = {'ticks': 0, 'minflt': 0, 'majflt': 0, 'rss_kib': 0, 'hwm_kib': 0, 'voluntary_ctxt': 0, 'nonvoluntary_ctxt': 0, 'threads': 0, 'live_pids': 0}
    for p in set((int(x) for x in pids if int(x) > 0)):
        a = _stat(p)
        b = _status(p)
        if not a:
            continue
        o['live_pids'] += 1
        for k in ('ticks', 'minflt', 'majflt', 'threads'):
            o[k] += a[k]
        if b:
            for k in ('rss_kib', 'hwm_kib', 'voluntary_ctxt', 'nonvoluntary_ctxt'):
                o[k] += b[k]
    o['cpu_ns'] = int(o['ticks'] / CLK * 1000000000.0)
    return o

def system_snapshot():
    o = {'ctxt': None, 'procs_running': None, 'load1': None, 'run_queue': None, 'net_rx_bytes': None, 'net_tx_bytes': None}
    try:
        for l in Path('/proc/stat').read_text().splitlines():
            if l.startswith('ctxt '):
                o['ctxt'] = int(l.split()[1])
            elif l.startswith('procs_running '):
                o['procs_running'] = int(l.split()[1])
    except OSError:
        pass
    try:
        p = Path('/proc/loadavg').read_text().split()
        o['load1'] = float(p[0])
        o['run_queue'] = int(p[3].split('/')[0])
    except Exception:
        pass
    try:
        rx = tx = 0
        for l in Path('/proc/net/dev').read_text().splitlines()[2:]:
            f = l.split(':', 1)[1].split()
            rx += int(f[0])
            tx += int(f[8])
        o['net_rx_bytes'] = rx
        o['net_tx_bytes'] = tx
    except Exception:
        pass
    return o

def delta_snapshot(a, b, wall_ns, cpus):
    d = lambda k: None if a.get(k) is None or b.get(k) is None else int(b[k]) - int(a[k])
    cpu = d('cpu_ns') or 0
    return {'wall_ns': wall_ns, 'process_cpu_ns': cpu, 'cpu_utilization_pct_of_allocated': 100 * cpu / max(1, wall_ns * max(1, cpus)), 'minor_faults': d('minflt'), 'major_faults': d('majflt'), 'voluntary_context_switches': d('voluntary_ctxt'), 'nonvoluntary_context_switches': d('nonvoluntary_ctxt'), 'rss_kib_end': b.get('rss_kib'), 'hwm_kib_end': b.get('hwm_kib'), 'threads_end': b.get('threads')}

@dataclass
class RuntimeSampler:
    pids: list[int]
    interval_s: float = 0.1
    samples: list[dict[str, Any]] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        st = time.perf_counter_ns()
        while not self.stop_event.wait(self.interval_s):
            self.samples.append({'elapsed_ns': time.perf_counter_ns() - st, **process_snapshot(self.pids), **system_snapshot()})

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2) if self.thread else None

        def vals(k):
            return [float(x[k]) for x in self.samples if x.get(k) is not None]
        rq, pr, rss, hwm = (vals('run_queue'), vals('procs_running'), vals('rss_kib'), vals('hwm_kib'))
        return {'sample_count': len(self.samples), 'run_queue_mean': sum(rq) / len(rq) if rq else None, 'run_queue_max': max(rq) if rq else None, 'procs_running_mean': sum(pr) / len(pr) if pr else None, 'procs_running_max': max(pr) if pr else None, 'rss_kib_max': max(rss) if rss else None, 'hwm_kib_max': max(hwm) if hwm else None}
