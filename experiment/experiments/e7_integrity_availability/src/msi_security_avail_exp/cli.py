from __future__ import annotations
import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any
from . import __version__
from .analysis import analyze, inspect_blocks, missing_block_indices, validate_results
from .availability import run_block as run_e8_block
from .crypto import Ed25519OpenSSL
from .e7_runner import run_block as run_e7_block
from .plan import build_plan
from .util import atomic_write_json, read_jsonl, system_info

def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))

def cmd_info(_args: argparse.Namespace) -> int:
    openssl: dict[str, Any]
    try:
        openssl = Ed25519OpenSSL().metadata()
    except Exception as exc:
        openssl = {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
    value = {'experiment': 'E7+E8', 'version': __version__, 'python_supported': sys.version_info >= (3, 10), 'python_version': platform.python_version(), 'python_executable': sys.executable, 'openssl_ed25519': openssl, 'dependencies': ['Python standard library', 'system libcrypto with Ed25519', 'Slurm for HPC orchestration'], 'environment': system_info()}
    _print_json(value)
    return 0 if value['python_supported'] and openssl.get('available') else 69

def cmd_plan(args: argparse.Namespace) -> int:
    summary = build_plan(Path(args.config).resolve(), Path(args.output).resolve())
    _print_json(summary)
    return 0

def cmd_run_e7(args: argparse.Namespace) -> int:
    path = run_e7_block(Path(args.plan_dir).resolve(), int(args.index), Path(args.raw).resolve())
    print(path)
    return 0

def cmd_run_e8(args: argparse.Namespace) -> int:
    path = run_e8_block(Path(args.plan_dir).resolve(), int(args.index), Path(args.raw).resolve())
    print(path)
    return 0

def cmd_analyze(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    summary = analyze(run_dir, Path(args.raw_e7).resolve() if args.raw_e7 else run_dir / 'raw_e7', Path(args.raw_e8).resolve() if args.raw_e8 else run_dir / 'raw_e8', Path(args.results).resolve() if args.results else run_dir / 'results', allow_incomplete=bool(args.allow_incomplete))
    _print_json(summary)
    return 0 if summary['status'] == 'PASS' else 1

def cmd_validate(args: argparse.Namespace) -> int:
    ok, errors = validate_results(Path(args.results).resolve())
    if ok:
        print('PASS')
        return 0
    print('FAIL')
    for error in errors:
        print(error, file=sys.stderr)
    return 1

def cmd_inspect(args: argparse.Namespace) -> int:
    audit = inspect_blocks(Path(args.plan_dir).resolve(), Path(args.raw).resolve(), args.experiment.upper())
    audit.pop('payloads', None)
    _print_json(audit)
    return 0 if audit['complete'] else 1

def cmd_missing(args: argparse.Namespace) -> int:
    values = missing_block_indices(Path(args.plan_dir).resolve(), Path(args.raw).resolve(), args.experiment.upper())
    print(','.join((str(value) for value in values)))
    return 0

def cmd_run_local(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    if output.exists() and args.clean:
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    for name in ('raw_e7', 'raw_e8', 'results', 'logs'):
        (output / name).mkdir(parents=True, exist_ok=True)
    summary = build_plan(Path(args.config).resolve(), output)
    e7_blocks = read_jsonl(output / 'e7_blocks.jsonl')
    e8_blocks = read_jsonl(output / 'e8_blocks.jsonl')
    for index in range(len(e7_blocks)):
        run_e7_block(output, index, output / 'raw_e7')
    for index in range(len(e8_blocks)):
        run_e8_block(output, index, output / 'raw_e8')
    result = analyze(output, output / 'raw_e7', output / 'raw_e8', output / 'results')
    atomic_write_json(output / 'local_run_summary.json', {'plan': summary, 'result': result})
    _print_json(result)
    return 0 if result['status'] == 'PASS' else 1

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='exp07_security_availability', description='Experiment 7 integrity and availability benchmark')
    sub = root.add_subparsers(dest='command', required=True)
    p = sub.add_parser('info', help='probe Python and Ed25519 support')
    p.set_defaults(func=cmd_info)
    p = sub.add_parser('plan', help='materialize deterministic E7 and E8 plans')
    p.add_argument('--config', required=True)
    p.add_argument('--output', required=True)
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser('run-e7-block', help='execute one E7 Slurm block')
    p.add_argument('--plan-dir', required=True)
    p.add_argument('--index', required=True, type=int)
    p.add_argument('--raw', required=True)
    p.set_defaults(func=cmd_run_e7)
    p = sub.add_parser('run-e8-block', help='execute one E8 Slurm block')
    p.add_argument('--plan-dir', required=True)
    p.add_argument('--index', required=True, type=int)
    p.add_argument('--raw', required=True)
    p.set_defaults(func=cmd_run_e8)
    p = sub.add_parser('analyze', help='validate raw blocks and produce CSV/SVG results')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--raw-e7')
    p.add_argument('--raw-e8')
    p.add_argument('--results')
    p.add_argument('--allow-incomplete', action='store_true')
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser('validate', help='verify result structure and SHA-256 manifest')
    p.add_argument('--results', required=True)
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser('inspect', help='inspect E7 or E8 block completeness')
    p.add_argument('--experiment', choices=('E7', 'E8', 'e7', 'e8'), required=True)
    p.add_argument('--plan-dir', required=True)
    p.add_argument('--raw', required=True)
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser('missing-blocks', help='print missing/corrupt array indices as a Slurm list')
    p.add_argument('--experiment', choices=('E7', 'E8', 'e7', 'e8'), required=True)
    p.add_argument('--plan-dir', required=True)
    p.add_argument('--raw', required=True)
    p.set_defaults(func=cmd_missing)
    p = sub.add_parser('run-local', help='execute a complete smoke/reference plan sequentially')
    p.add_argument('--config', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--clean', action='store_true')
    p.set_defaults(func=cmd_run_local)
    return root

def main(argv: list[str] | None=None) -> int:
    if sys.version_info < (3, 10):
        print('ERROR: Python 3.10 or newer is required.', file=sys.stderr)
        return 69
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print('Interrupted', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        if os.environ.get('EXP07_DEBUG') == '1':
            raise
        return 1
