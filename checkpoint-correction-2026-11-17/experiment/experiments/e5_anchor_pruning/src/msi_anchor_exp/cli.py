from __future__ import annotations
import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any
from . import __version__
from .analysis import analyze, inspect_raw, missing_case_indices, validate_results
from .benchmark import run_case
from .config import count_plan, dataset_spec_from_config, make_plan
from .crypto import openssl_info
from .dataset import DatasetSpec, build_dataset, dataset_path, validate_dataset
from .util import atomic_write_json, jsonl_at, jsonl_iter, system_metadata

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp05_anchor', description='Experiment E5: certified root anchoring and safe pruning.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='Report runtime and cryptographic support.')
    command = sub.add_parser('make-plan', help='Generate a deterministic JSONL plan.')
    command.add_argument('--config', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('count-plan', help='Count cases or work units in a plan.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--kind', choices=('cases', 'configurations', 'query_measurements', 'pruned_epoch_inputs'), default='cases')
    command = sub.add_parser('dataset-spec', help='Write the accumulator dataset specification.')
    command.add_argument('--config', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('prepare-dataset', help='Build the read-only accumulator dataset.')
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path)
    source.add_argument('--spec-json', type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--force', action='store_true')
    command.add_argument('--verify-hashes', action='store_true')
    command = sub.add_parser('validate-dataset', help='Validate the prepared accumulator dataset.')
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path)
    source.add_argument('--spec-json', type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--verify-hashes', action='store_true')
    command = sub.add_parser('get-case', help='Extract one zero-based plan entry.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--index', required=True, type=int)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('run-case', help='Execute one case JSON file.')
    command.add_argument('--case-json', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command = sub.add_parser('run-index', help='Execute one zero-based plan entry.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--index', required=True, type=int)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command = sub.add_parser('run-plan', help='Run all cases sequentially; intended for smoke/reference only.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--dataset-dir', required=True, type=Path)
    command.add_argument('--raw-dir', required=True, type=Path)
    command = sub.add_parser('inspect-raw', help='Audit raw result completeness and checks.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command = sub.add_parser('missing-indices', help='Print comma-separated Slurm indices that need recovery.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command = sub.add_parser('analyze', help='Generate tables, figures, hypothesis checks, and manifest.')
    command.add_argument('--plan', required=True, type=Path)
    command.add_argument('--raw', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = sub.add_parser('validate', help='Validate an analyzed result directory.')
    command.add_argument('--results', required=True, type=Path)
    return parser

def _load_object(path: Path) -> dict[str, Any]:
    with path.open(encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'expected JSON object in {path}')
    return value

def _dataset_spec(config: Path | None, spec_json: Path | None) -> DatasetSpec:
    if config is not None:
        return dataset_spec_from_config(config)
    if spec_json is not None:
        return DatasetSpec.from_dict(_load_object(spec_json))
    raise AssertionError('dataset specification source was not provided')

def _apply_runtime_overrides(case: dict[str, Any]) -> dict[str, Any]:
    result = dict(case)
    if os.environ.get('E5_DISABLE_PINNING', '0') == '1':
        result['disable_pinning'] = True
    return result

def main(argv: list[str] | None=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'info':
        info = {'artifact': 'Experiment E5 — Certified Root Anchoring and Safe Pruning', 'version': __version__, 'python_supported': sys.version_info >= (3, 10), 'python_version': platform.python_version(), 'openssl_ed25519': openssl_info(), 'system': system_metadata()}
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0 if info['python_supported'] and info['openssl_ed25519'].get('available') else 69
    if args.command == 'make-plan':
        print(json.dumps(make_plan(args.config, args.output), indent=2, sort_keys=True))
        return 0
    if args.command == 'count-plan':
        print(count_plan(args.plan, args.kind))
        return 0
    if args.command == 'dataset-spec':
        spec = dataset_spec_from_config(args.config)
        atomic_write_json(args.output, spec.as_dict())
        print(json.dumps(spec.as_dict(), indent=2, sort_keys=True))
        return 0
    if args.command == 'prepare-dataset':
        spec = _dataset_spec(args.config, args.spec_json)
        result = build_dataset(args.dataset_dir, spec, force=args.force)
        if args.verify_hashes:
            result = validate_dataset(dataset_path(args.dataset_dir, spec), spec, verify_hashes=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result['status'] == 'PASS' else 1
    if args.command == 'validate-dataset':
        spec = _dataset_spec(args.config, args.spec_json)
        result = validate_dataset(dataset_path(args.dataset_dir, spec), spec, verify_hashes=args.verify_hashes)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result['status'] == 'PASS' else 1
    if args.command == 'get-case':
        case = jsonl_at(args.plan, args.index)
        atomic_write_json(args.output, case)
        print(case['case_id'])
        return 0
    if args.command == 'run-case':
        case = _apply_runtime_overrides(_load_object(args.case_json))
        run_case(case, args.dataset_dir, args.raw_dir)
        print(json.dumps({'case_id': case['case_id'], 'status': 'PASS'}, sort_keys=True))
        return 0
    if args.command == 'run-index':
        case = _apply_runtime_overrides(jsonl_at(args.plan, args.index))
        run_case(case, args.dataset_dir, args.raw_dir)
        print(json.dumps({'case_id': case['case_id'], 'status': 'PASS'}, sort_keys=True))
        return 0
    if args.command == 'run-plan':
        completed = []
        for case in jsonl_iter(args.plan):
            case = _apply_runtime_overrides(case)
            run_case(case, args.dataset_dir, args.raw_dir)
            completed.append(case['case_id'])
        print(json.dumps({'status': 'PASS', 'completed_cases': len(completed)}, indent=2, sort_keys=True))
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
