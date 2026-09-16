from __future__ import annotations
import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any
from . import __version__
from .benchmark import result_filename, write_case_result
from .config import load_config, make_plan, read_plan, write_plan
from .crypto import CycleCounter, Ed25519OpenSSL, RaplEnergyMeter
from .util import read_json

def _valid_result(path: Path, case_id: str) -> bool:
    try:
        obj = read_json(path)
    except Exception:
        return False
    return obj.get('schema_version') == 1 and obj.get('experiment') == 'E2' and (obj.get('case', {}).get('case_id') == case_id) and isinstance(obj.get('checks'), dict) and all(obj['checks'].values())

def _load_case(plan: Path, index: int, case_index: int) -> dict[str, Any]:
    blocks = read_plan(plan)
    if index < 0 or index >= len(blocks):
        raise IndexError(f'plan index {index} outside [0,{len(blocks) - 1}]')
    cases = blocks[index]['cases']
    if case_index < 0 or case_index >= len(cases):
        raise IndexError(f'case index {case_index} outside [0,{len(cases) - 1}]')
    return cases[case_index]

def cmd_info(_args: argparse.Namespace) -> int:
    backend_status: dict[str, Any]
    try:
        backend = Ed25519OpenSSL()
        backend_status = {'available': True, 'library': backend.library, 'version': backend.version(), 'ed25519_nid': backend.nid}
    except Exception as exc:
        backend_status = {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
    cycles = CycleCounter()
    cycle_meta = cycles.metadata()
    cycles.close()
    energy = RaplEnergyMeter().metadata()
    print(json.dumps({'experiment': 'E2', 'version': __version__, 'python': sys.version, 'openssl_ed25519': backend_status, 'cycles': cycle_meta, 'rapl': energy, 'third_party_python_dependencies': []}, indent=2, sort_keys=True))
    return 0 if backend_status['available'] else 69

def cmd_make_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    blocks = make_plan(config)
    summary = write_plan(args.output, blocks)
    print(json.dumps({'plan': str(args.output), **summary}, sort_keys=True))
    return 0

def cmd_count_plan(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    if args.kind == 'blocks':
        print(len(blocks))
    else:
        print(sum((len(block['cases']) for block in blocks)))
    return 0

def cmd_run_case(args: argparse.Namespace) -> int:
    case = _load_case(args.plan, args.index, args.case_index)
    args.output.mkdir(parents=True, exist_ok=True)
    path = write_case_result(case, args.output)
    print(json.dumps({'status': 'completed', 'case_id': case['case_id'], 'output': str(path)}, sort_keys=True))
    return 0

def cmd_run_block(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    if args.index < 0 or args.index >= len(blocks):
        raise IndexError(f'plan index {args.index} outside [0,{len(blocks) - 1}]')
    block = blocks[args.index]
    args.output.mkdir(parents=True, exist_ok=True)
    indices = list(range(len(block['cases'])))
    random.Random(int(block['case_order_seed'])).shuffle(indices)
    launcher = args.launcher.resolve() if args.launcher else Path(sys.argv[0]).resolve()
    completed = 0
    skipped = 0
    for case_index in indices:
        case = block['cases'][case_index]
        path = args.output / result_filename(case)
        if not args.force and _valid_result(path, str(case['case_id'])):
            skipped += 1
            continue
        env = os.environ.copy()
        env.setdefault('PYTHONHASHSEED', '0')
        env.setdefault('OMP_NUM_THREADS', '1')
        env.setdefault('OPENBLAS_NUM_THREADS', '1')
        env.setdefault('MKL_NUM_THREADS', '1')
        command = [sys.executable, str(launcher), '_run-case', '--plan', str(args.plan.resolve()), '--index', str(args.index), '--case-index', str(case_index), '--output', str(args.output.resolve())]
        subprocess.run(command, check=True, env=env)
        completed += 1
    print(json.dumps({'block_id': block['block_id'], 'plan_index': args.index, 'completed': completed, 'skipped': skipped, 'cases': len(indices)}, sort_keys=True))
    return 0

def cmd_run_plan(args: argparse.Namespace) -> int:
    blocks = read_plan(args.plan)
    launcher = args.launcher.resolve() if args.launcher else Path(sys.argv[0]).resolve()
    for index in range(len(blocks)):
        namespace = argparse.Namespace(plan=args.plan, index=index, output=args.output, launcher=launcher, force=args.force)
        cmd_run_block(namespace)
    return 0

def cmd_analyze(args: argparse.Namespace) -> int:
    from .analysis import analyze
    analyze(args.raw, args.output, args.plan)
    return 0

def cmd_validate(args: argparse.Namespace) -> int:
    summary = read_json(args.results / 'summary.json')
    required = [args.results / 'report.md', args.results / 'latency_summary.csv', args.results / 'phase_summary.csv', args.results / 'throughput_resources.csv', args.results / 'model_fits.csv', args.results / 'hypothesis_checks.csv', args.results / 'manifest.sha256']
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f'missing result artifacts: {missing}')
    if not summary.get('plan_audit', {}).get('complete'):
        raise RuntimeError('plan audit is incomplete')
    if not summary.get('internal_checks_pass'):
        raise RuntimeError('at least one raw-result internal check failed')
    print('PASS')
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp02_query', description='Experiment E2 local verified-query computation benchmark')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('info', help='show dependency and hardware-counter availability')
    p.set_defaults(func=cmd_info)
    p = sub.add_parser('make-plan', help='expand a JSON configuration into JSONL blocks')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(func=cmd_make_plan)
    p = sub.add_parser('count-plan', help='count Slurm blocks or child process cases')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--kind', choices=('blocks', 'cases'), default='blocks')
    p.set_defaults(func=cmd_count_plan)
    p = sub.add_parser('run-block', help='run one randomized paired Slurm block')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--launcher', type=Path)
    p.add_argument('--force', action='store_true')
    p.set_defaults(func=cmd_run_block)
    p = sub.add_parser('run-plan', help='run every block locally using child processes')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--launcher', type=Path)
    p.add_argument('--force', action='store_true')
    p.set_defaults(func=cmd_run_plan)
    p = sub.add_parser('_run-case', help=argparse.SUPPRESS)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--case-index', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(func=cmd_run_case)
    p = sub.add_parser('analyze', help='aggregate raw launch results')
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.set_defaults(func=cmd_analyze)
    p = sub.add_parser('validate', help='validate an analyzed result directory')
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
