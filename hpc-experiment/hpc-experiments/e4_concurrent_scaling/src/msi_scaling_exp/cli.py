from __future__ import annotations
import argparse
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any
from . import __version__
from .analysis import analyze, inspect_raw, missing_case_indices, validate_results
from .benchmark import run_case
from .config import count_plan, make_plan, unique_datasets
from .crypto import openssl_info
from .index import DatasetSpec, build_dataset, validate_dataset
from .service import ServiceConfig, serve
from .util import atomic_write_json, jsonl_at, jsonl_iter, system_metadata, write_jsonl

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp04_scaling.pyz', description='Experiment E4: concurrent multi-shard and long-history scaling.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='Report runtime, OpenSSL, and host information.')
    command = sub.add_parser('make-plan', help='Generate the deterministic case plan.')
    command.add_argument('--config', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('count-plan', help='Count plan cases, datasets, or rate steps.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--kind', choices=('cases', 'datasets', 'rate_steps'), default='cases')
    command = sub.add_parser('make-dataset-plan', help='Extract unique dataset specifications.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('get-case', help='Write one case from a plan to JSON.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--index', required=True, type=int)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('get-dataset', help='Write one dataset specification to JSON.')
    command.add_argument('--dataset-plan', required=True, type=Path)
    command.add_argument('--index', required=True, type=int)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('prepare-dataset', help='Build and validate one fixed-width MSI index.')
    command.add_argument('--spec-json', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--workers', type=int, default=1)
    command.add_argument('--force', action='store_true')
    command = sub.add_parser('prepare-all', help='Build all unique datasets in a plan sequentially.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--workers', type=int, default=1)
    command = sub.add_parser('run-case', help='Run one case supplied as JSON.')
    command.add_argument('--case-json', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command.add_argument('--external-ready', type=Path)
    command = sub.add_parser('run-index', help='Run one case by zero-based plan index.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--index', required=True, type=int)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command.add_argument('--external-ready', type=Path)
    command = sub.add_parser('run-plan', help='Run every plan case sequentially (smoke/reference only).')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command = sub.add_parser('service', help='Run an external retrieval service for a dual-node case.')
    command.add_argument('--case-json', required=True, type=Path)
    command.add_argument('--bind-host', default='0.0.0.0')
    command.add_argument('--port', type=int, default=0)
    command.add_argument('--advertise-host')
    command.add_argument('--ready', required=True, type=Path)
    command.add_argument('--final-stats', required=True, type=Path)
    command = sub.add_parser('inspect-raw', help='Audit raw case completeness and integrity.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command = sub.add_parser('missing-indices', help='Print Slurm array indices requiring recovery.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command = sub.add_parser('analyze', help='Generate tables, figures, checks, and manifest.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('validate', help='Validate an analyzed results directory.')
    command.add_argument('--results', required=True, type=Path)
    return parser

def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'expected a JSON object in {path}')
    return value

def _dataset_plan(plan: Path, output: Path) -> dict[str, Any]:
    specs = sorted(unique_datasets(plan), key=lambda spec: spec.dataset_id)
    rows = [{'dataset_id': spec.dataset_id, **spec.as_dict()} for spec in specs]
    write_jsonl(output, rows)
    return {'schema_version': 1, 'experiment': 'E4', 'datasets': len(rows), 'total_records': sum((spec.total_records for spec in specs)), 'total_logical_bytes': sum((4096 + 64 * spec.total_records for spec in specs)), 'dataset_ids': [spec.dataset_id for spec in specs]}

def _rate_step_count(plan: Path) -> int:
    count = 0
    for row in jsonl_iter(plan):
        count += len(row['rate_multipliers']) if row['trial_kind'] == 'ramp' else 1
    return count

def main(argv: list[str] | None=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'info':
        info = {'artifact': 'Experiment E4 — Concurrent Multi-Shard and Long-History Scaling', 'version': __version__, 'python_supported': sys.version_info >= (3, 10), 'python_version': platform.python_version(), 'openssl_ed25519': openssl_info(), 'system': system_metadata()}
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0 if info['python_supported'] and info['openssl_ed25519'].get('available') else 69
    if args.command == 'make-plan':
        summary = make_plan(args.config, args.output)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if args.command == 'count-plan':
        if args.kind == 'cases':
            value = count_plan(args.plan)
        elif args.kind == 'datasets':
            value = len(unique_datasets(args.plan))
        else:
            value = _rate_step_count(args.plan)
        print(value)
        return 0
    if args.command == 'make-dataset-plan':
        print(json.dumps(_dataset_plan(args.plan, args.output), indent=2, sort_keys=True))
        return 0
    if args.command == 'get-case':
        case = jsonl_at(args.plan, args.index)
        atomic_write_json(args.output, case)
        print(case['case_id'])
        return 0
    if args.command == 'get-dataset':
        value = jsonl_at(args.dataset_plan, args.index)
        atomic_write_json(args.output, value)
        print(value['dataset_id'])
        return 0
    if args.command == 'prepare-dataset':
        spec = DatasetSpec.from_dict(_load_json(args.spec_json))
        result = build_dataset(args.dataset_dir, spec, workers=max(1, args.workers), force=args.force)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == 'prepare-all':
        results = []
        for spec in unique_datasets(args.plan):
            results.append(build_dataset(args.dataset_dir, spec, workers=max(1, args.workers)))
        print(json.dumps({'status': 'PASS', 'datasets': results}, indent=2, sort_keys=True))
        return 0
    if args.command == 'run-case':
        case = _load_json(args.case_json)
        result = run_case(case, args.dataset_dir, args.raw_dir, external_ready_file=args.external_ready)
        passed = result['checks']['all_queries_accepted'] and result['checks']['hash_calls_exact'] and all((code == 0 for code in result['checks']['worker_exit_codes']))
        print(json.dumps({'case_id': case['case_id'], 'status': 'PASS' if passed else 'FAIL'}, sort_keys=True))
        return 0 if passed else 1
    if args.command == 'run-index':
        case = jsonl_at(args.plan, args.index)
        result = run_case(case, args.dataset_dir, args.raw_dir, external_ready_file=args.external_ready)
        passed = result['checks']['all_queries_accepted'] and result['checks']['hash_calls_exact'] and all((code == 0 for code in result['checks']['worker_exit_codes']))
        print(json.dumps({'case_id': case['case_id'], 'status': 'PASS' if passed else 'FAIL'}, sort_keys=True))
        return 0 if passed else 1
    if args.command == 'run-plan':
        completed = []
        for case in jsonl_iter(args.plan):
            result = run_case(case, args.dataset_dir, args.raw_dir)
            completed.append(result['case']['case_id'])
        print(json.dumps({'status': 'PASS', 'completed_cases': completed}, indent=2, sort_keys=True))
        return 0
    if args.command == 'service':
        case = _load_json(args.case_json)
        spec = DatasetSpec.from_dict(case)
        config = ServiceConfig(spec=spec, bandwidth_mbps_per_connection=float(case['bandwidth_mbps']), service_cpus=int(case['service_cpus']), key_seed=int(case['anchor_key_seed']))
        serve(config, bind_host=args.bind_host, port=args.port, ready_file=args.ready, final_stats_file=args.final_stats, advertise_host=args.advertise_host)
        return 0
    if args.command == 'inspect-raw':
        audit = inspect_raw(args.plan, args.raw)
        audit.pop('results', None)
        print(json.dumps(audit, indent=2, sort_keys=True))
        return 0 if audit['complete'] else 1
    if args.command == 'missing-indices':
        print(','.join((str(index) for index in missing_case_indices(args.plan, args.raw))))
        return 0
    if args.command == 'analyze':
        summary = analyze(args.plan, args.raw, args.output)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary['status'] == 'PASS' else 1
    if args.command == 'validate':
        result = validate_results(args.results)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result['status'] == 'PASS' else 1
    raise AssertionError('unreachable')
