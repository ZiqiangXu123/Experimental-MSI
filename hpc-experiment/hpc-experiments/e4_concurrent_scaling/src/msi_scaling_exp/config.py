from __future__ import annotations
import hashlib, itertools, json
from pathlib import Path
from typing import Any
from .index import DatasetSpec
from .util import read_json, stable_seed, write_jsonl

def _merge(*parts):
    out = {}
    for p in parts:
        out.update(p)
    return out

def _key(c):
    ignore = {'sweep_tags', 'primary_sweep', 'trial', 'case_id', 'seed'}
    return json.dumps({k: v for k, v in c.items() if k not in ignore}, sort_keys=True, separators=(',', ':'))

def make_configurations(cfg: dict[str, Any]):
    d = cfg['defaults']
    base = _merge(d, cfg['base'])
    items = {}

    def add(tag, over=None):
        c = _merge(base, over or {})
        c['sweep_tags'] = [tag]
        c['primary_sweep'] = tag
        k = _key(c)
        if k in items:
            if tag not in items[k]['sweep_tags']:
                items[k]['sweep_tags'].append(tag)
        else:
            items[k] = c
    s = cfg['sweeps']
    for c, dist in itertools.product(s['concurrency_ramp']['concurrent_clients'], s['concurrency_ramp']['distributions']):
        add('concurrency_ramp', {'concurrent_clients': c, 'distribution': dist, 'trial_kind': 'ramp'})
    for workers, dist in itertools.product(s['core_scaling']['verifier_workers'], s['core_scaling']['distributions']):
        add('core_scaling', {'concurrent_clients': s['core_scaling']['concurrent_clients'], 'verifier_workers': workers, 'distribution': dist, 'trial_kind': 'ramp'})
    for t, dist in itertools.product(s['history_axis']['historical_epochs'], s['history_axis']['distributions']):
        add('history_axis', {'historical_epochs': t, 'distribution': dist, 'trial_kind': 'steady'})
    for n, dist in itertools.product(s['shard_axis']['shard_count'], s['shard_axis']['distributions']):
        add('shard_axis', {'shard_count': n, 'distribution': dist, 'trial_kind': 'steady'})
    f = s['factorial']
    for n, t, c, dist in itertools.product(f['shard_count'], f['historical_epochs'], f['concurrent_clients'], f['distributions']):
        add('factorial', {'shard_count': n, 'historical_epochs': t, 'concurrent_clients': c, 'distribution': dist, 'trial_kind': 'steady'})
    for dist in s['workload']['distributions']:
        add('workload', {'distribution': dist, 'trial_kind': 'ramp'})
    m = s['mode_sensitivity']
    for scale, mode, dist in itertools.product(m['scales'], m['modes'], m['distributions']):
        add('mode_sensitivity', {'shard_count': int(scale['shard_count']), 'historical_epochs': int(scale['historical_epochs']), 'mode': mode, 'distribution': dist, 'trial_kind': 'steady'})
    out = list(items.values())
    for c in out:
        c['sweep_tags'] = sorted(c['sweep_tags'])
        c['primary_sweep'] = c['sweep_tags'][0]
    return sorted(out, key=lambda c: _key(c))

def make_plan(config_path: str | Path, output_path: str | Path):
    cfg = read_json(config_path)
    configs = make_configurations(cfg)
    rows = []
    trials = int(cfg['trial_count'])
    plan_seed = int(cfg['plan_seed'])
    for ci, c in enumerate(configs):
        config_material = _key(c)
        config_id = hashlib.sha256(config_material.encode()).hexdigest()[:16]
        for trial in range(trials):
            row = dict(c)
            row.update({'schema_version': 1, 'experiment': 'E4', 'config_id': config_id, 'trial': trial, 'seed': stable_seed(plan_seed, config_id, trial)})
            row['case_id'] = f"{row['primary_sweep']}-{config_id}-t{trial:02d}"
            rows.append(row)
    rows.sort(key=lambda x: x['case_id'])
    write_jsonl(output_path, rows)
    datasets = {DatasetSpec.from_dict(r).dataset_id: DatasetSpec.from_dict(r).as_dict() for r in rows}
    summary = {'schema_version': 1, 'experiment': 'E4', 'configurations': len(configs), 'cases': len(rows), 'datasets': len(datasets), 'trials_per_configuration': trials, 'dataset_specs': list(datasets.values()), 'sweep_configuration_counts': {tag: sum((1 for c in configs if tag in c['sweep_tags'])) for tag in cfg['sweeps']}}
    expected = cfg.get('expected', {})
    for k in ('configurations', 'cases', 'datasets'):
        if k in expected and int(expected[k]) != summary[k]:
            raise ValueError(f'publication plan changed: {k}={summary[k]} expected={expected[k]}')
    return summary

def count_plan(plan_path):
    return sum((1 for _ in open(plan_path, encoding='utf-8') if _.strip()))

def unique_datasets(plan_path):
    from .util import jsonl_iter
    out = {}
    for r in jsonl_iter(plan_path):
        s = DatasetSpec.from_dict(r)
        out[s.dataset_id] = s
    return list(out.values())
