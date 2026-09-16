from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from . import __version__
from .analysis import analyze, validate_results
from .benchmark import run_point
from .config import count_plan, expand_config, load_config, read_plan_point, write_plan
from .constants import ANCHOR_RECORD_BYTES, DIGEST_BYTES, MSI_ENTRY_BYTES, MSI_METADATA_BYTES
from .util import environment_inventory

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp01_storage', description='Experiment E1: MSI verifier-resident and auxiliary storage scaling')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='print implementation and environment information')
    p = sub.add_parser('make-plan', help='expand a JSON configuration into a stable JSONL plan')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('count-plan', help='print the number of points in a plan')
    p.add_argument('--plan', type=Path, required=True)
    p = sub.add_parser('run-point', help='execute one plan point')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--scratch', default=None)
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('run-plan', help='execute every point serially (local validation only)')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--scratch', default=None)
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('analyze', help='aggregate raw JSON results and generate tables/figures')
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--plan', type=Path, action='append', default=None, help='submitted JSONL plan; repeat for appended plan sets')
    p = sub.add_parser('validate', help='validate a generated result directory')
    p.add_argument('--results', type=Path, required=True)
    return parser

def main(argv: list[str] | None=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'info':
        print(json.dumps({'version': __version__, 'digest_bytes': DIGEST_BYTES, 'msi_metadata_bytes': MSI_METADATA_BYTES, 'msi_entry_bytes': MSI_ENTRY_BYTES, 'anchor_record_bytes': ANCHOR_RECORD_BYTES, 'environment': environment_inventory()}, indent=2, sort_keys=True))
        return 0
    if args.command == 'make-plan':
        points = expand_config(load_config(args.config))
        write_plan(points, args.output)
        print(f'PLAN={args.output}')
        print(f'POINTS={len(points)}')
        print(f'LAST_INDEX={len(points) - 1}')
        return 0
    if args.command == 'count-plan':
        print(count_plan(args.plan))
        return 0
    if args.command == 'run-point':
        point = read_plan_point(args.plan, args.index)
        path = run_point(point, output_dir=args.output, scratch_base=args.scratch, force=args.force)
        print(path)
        return 0
    if args.command == 'run-plan':
        count = count_plan(args.plan)
        for index in range(count):
            point = read_plan_point(args.plan, index)
            path = run_point(point, output_dir=args.output, scratch_base=args.scratch, force=args.force)
            print(f'[{index + 1}/{count}] {path}')
        return 0
    if args.command == 'analyze':
        summary = analyze(args.raw, args.output, plan_paths=args.plan)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary['all_hypotheses_pass'] else 2
    if args.command == 'validate':
        ok = validate_results(args.results)
        print('PASS' if ok else 'FAIL')
        return 0 if ok else 2
    raise AssertionError(args.command)
