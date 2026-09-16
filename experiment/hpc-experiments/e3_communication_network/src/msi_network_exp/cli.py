from __future__ import annotations
import argparse
import json
import platform
import random
import socket
import sys
from pathlib import Path
from typing import Any
from . import __version__
from .analysis import analyze, validate_results
from .benchmark import run_block_local, write_case_result
from .config import load_config, make_plan, read_plan, write_plan
from .crypto import Ed25519OpenSSL
from .service import run_service
from .util import read_json

def _load_case_file(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding='utf-8'))
    if obj.get('experiment') != 'E3' or obj.get('schema_version') != 1:
        raise ValueError('invalid E3 case file')
    return obj

def cmd_info(_args: argparse.Namespace) -> int:
    crypto: dict[str, Any]
    try:
        backend = Ed25519OpenSSL()
        crypto = {'available': True, 'library': backend.library, 'version': backend.version()}
    except Exception as exc:
        crypto = {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
    obj = {'experiment': 'E3', 'version': __version__, 'python': platform.python_version(), 'platform': platform.platform(), 'hostname': socket.gethostname(), 'openssl_ed25519': crypto, 'dependencies': 'Python standard library plus system libcrypto; no pip/conda packages', 'link_backend': 'rootless calibrated userspace delay/bandwidth/virtual-loss emulator over persistent TCP'}
    print(json.dumps(obj, indent=2, sort_keys=True))
    return 0 if crypto.get('available') else 2

def cmd_make_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    blocks = make_plan(config)
    summary = write_plan(args.output, blocks)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0

def cmd_count_plan(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    if args.kind == 'blocks':
        value = len(blocks)
    elif args.kind == 'cases':
        value = sum((len(block['cases']) for block in blocks))
    elif args.kind == 'network':
        value = sum((case['kind'] == 'network_trial' for block in blocks for case in block['cases']))
    elif args.kind == 'wire':
        value = sum((case['kind'] == 'wire_audit' for block in blocks for case in block['cases']))
    else:
        raise ValueError('unsupported count kind')
    print(value)
    return 0

def cmd_extract_block(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    if args.index < 0 or args.index >= len(blocks):
        raise IndexError('plan index out of range')
    block = blocks[args.index]
    args.output.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    cases = list(block['cases'])
    random.Random(int(block['case_order_seed'])).shuffle(cases)
    for i, case in enumerate(cases):
        path = args.output / f'case-{i:02d}.json'
        path.write_text(json.dumps(case, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        paths.append(str(path))
    print(json.dumps({'block_id': block['block_id'], 'kind': block['kind'], 'case_files': paths}, indent=2))
    return 0

def cmd_serve(args: argparse.Namespace) -> int:
    case = _load_case_file(args.case_file)
    run_service(case, role=args.role, host=args.host, port=args.port, ready_file=args.ready_file, metrics_file=args.metrics_file, expected_queries=args.expected_queries)
    return 0

def _ready_arg(path: Path | None) -> dict[str, Any] | None:
    return read_json(path) if path is not None else None

def cmd_client_case(args: argparse.Namespace) -> int:
    case = _load_case_file(args.case_file)
    endpoints: dict[str, dict[str, Any]] = {}
    metrics: dict[str, str] = {}
    if case['kind'] == 'network_trial':
        if case.get('deployment') == 'coalesced':
            if args.gateway_ready is None or args.gateway_metrics is None:
                raise ValueError('coalesced client requires --gateway-ready and --gateway-metrics')
            endpoints['gateway'] = read_json(args.gateway_ready)
            metrics['gateway'] = str(args.gateway_metrics)
        else:
            if None in (args.payload_ready, args.payload_metrics, args.witness_ready, args.witness_metrics):
                raise ValueError('split client requires payload and witness ready/metrics paths')
            endpoints['payload'] = read_json(args.payload_ready)
            endpoints['witness'] = read_json(args.witness_ready)
            metrics['payload'] = str(args.payload_metrics)
            metrics['witness'] = str(args.witness_metrics)
    write_case_result(case, args.output, endpoints=endpoints or None, server_metric_files=metrics or None)
    return 0

def cmd_run_block(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    if args.index < 0 or args.index >= len(blocks):
        raise IndexError(f'plan index {args.index} outside [0,{len(blocks) - 1}]')
    paths = run_block_local(blocks[args.index], output_dir=args.output, launcher=args.launcher)
    print(json.dumps({'written': [str(p) for p in paths]}, indent=2))
    return 0

def cmd_analyze(args: argparse.Namespace) -> int:
    summary = analyze(args.raw, args.output, args.plan)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0

def cmd_validate(args: argparse.Namespace) -> int:
    print(json.dumps(validate_results(args.results), indent=2, sort_keys=True))
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp03_network', description='Experiment E3: communication cost and edge-network sensitivity')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('info')
    p.set_defaults(func=cmd_info)
    p = sub.add_parser('make-plan')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(func=cmd_make_plan)
    p = sub.add_parser('count-plan')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--kind', choices=('blocks', 'cases', 'network', 'wire'), required=True)
    p.set_defaults(func=cmd_count_plan)
    p = sub.add_parser('extract-block')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(func=cmd_extract_block)
    p = sub.add_parser('serve')
    p.add_argument('--case-file', type=Path, required=True)
    p.add_argument('--role', choices=('gateway', 'payload', 'witness'), required=True)
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--ready-file', type=Path, required=True)
    p.add_argument('--metrics-file', type=Path, required=True)
    p.add_argument('--expected-queries', type=int, required=True)
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser('client-case')
    p.add_argument('--case-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--gateway-ready', type=Path)
    p.add_argument('--gateway-metrics', type=Path)
    p.add_argument('--payload-ready', type=Path)
    p.add_argument('--payload-metrics', type=Path)
    p.add_argument('--witness-ready', type=Path)
    p.add_argument('--witness-metrics', type=Path)
    p.set_defaults(func=cmd_client_case)
    p = sub.add_parser('run-block')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--launcher', type=Path, required=True)
    p.set_defaults(func=cmd_run_block)
    p = sub.add_parser('analyze')
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser('validate')
    p.add_argument('--results', type=Path, required=True)
    p.set_defaults(func=cmd_validate)
    return parser

def main(argv: list[str] | None=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
if __name__ == '__main__':
    raise SystemExit(main())
