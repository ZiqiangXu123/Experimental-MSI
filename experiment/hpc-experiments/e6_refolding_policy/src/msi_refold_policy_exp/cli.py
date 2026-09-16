from __future__ import annotations
import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any
from .analysis import analyze_all, analyze_migration, validate_results, verify_manifest
from .catalog import build_catalog_from_prior_results, load_cost_catalog, validate_cost_catalog, write_reference_catalog
from .config import count_plan, make_plans
from .crypto import openssl_info
from .execution import build_dataset_index, inspect_raw, missing_block_indices, run_migration_block, run_policy_block, validate_all_datasets
from .util import atomic_write_json, jsonl_iter, pin_to_first_available_cpu, system_metadata

def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))

def _csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(',') if item.strip()]

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='exp06_refolding_policy', description='Experiment 6: root-preserving mode migration and resource-aware state allocation')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info')
    plan = sub.add_parser('plan')
    plan.add_argument('--config', required=True)
    plan.add_argument('--output', required=True)
    count = sub.add_parser('plan-count')
    count.add_argument('--config', required=True)
    dataset = sub.add_parser('build-dataset')
    dataset.add_argument('--plan', required=True)
    dataset.add_argument('--datasets', required=True)
    dataset.add_argument('--index', type=int, required=True)
    dataset.add_argument('--force', action='store_true')
    validate_dataset = sub.add_parser('validate-datasets')
    validate_dataset.add_argument('--plan', required=True)
    validate_dataset.add_argument('--datasets', required=True)
    validate_dataset.add_argument('--skip-hashes', action='store_true')
    migrate = sub.add_parser('run-migration-block')
    migrate.add_argument('--plan', required=True)
    migrate.add_argument('--blocks', required=True)
    migrate.add_argument('--block-index', type=int, required=True)
    migrate.add_argument('--datasets', required=True)
    migrate.add_argument('--raw', required=True)
    migrate.add_argument('--work-root', required=True)
    migrate.add_argument('--overwrite', action='store_true')
    missing_migration = sub.add_parser('missing-migration-blocks')
    missing_migration.add_argument('--plan', required=True)
    missing_migration.add_argument('--blocks', required=True)
    missing_migration.add_argument('--raw', required=True)
    inspect_migration = sub.add_parser('inspect-migration')
    inspect_migration.add_argument('--plan', required=True)
    inspect_migration.add_argument('--raw', required=True)
    analyze_e6 = sub.add_parser('analyze-migration')
    analyze_e6.add_argument('--plan', required=True)
    analyze_e6.add_argument('--raw', required=True)
    analyze_e6.add_argument('--output', required=True)
    analyze_e6.add_argument('--preferred-backend', default='node_local')
    reference_catalog = sub.add_parser('write-reference-catalog')
    reference_catalog.add_argument('--output', required=True)
    reference_catalog.add_argument('--n-values', required=True)
    catalog_build = sub.add_parser('build-component-catalog')
    catalog_build.add_argument('--e1-results', required=True)
    catalog_build.add_argument('--e2-results', required=True)
    catalog_build.add_argument('--e3-results', required=True)
    catalog_build.add_argument('--availability-csv', required=True)
    catalog_build.add_argument('--output', required=True)
    catalog_build.add_argument('--source-revision', required=True)
    catalog_build.add_argument('--deadline-ms', type=float, default=500.0)
    catalog_validate = sub.add_parser('validate-component-catalog')
    catalog_validate.add_argument('--catalog', required=True)
    catalog_validate.add_argument('--n-values', required=True)
    catalog_validate.add_argument('--require-publication', action='store_true')
    policy = sub.add_parser('run-policy-block')
    policy.add_argument('--plan', required=True)
    policy.add_argument('--blocks', required=True)
    policy.add_argument('--block-index', type=int, required=True)
    policy.add_argument('--component-costs', required=True)
    policy.add_argument('--migration-catalog', required=True)
    policy.add_argument('--raw', required=True)
    policy.add_argument('--overwrite', action='store_true')
    missing_policy = sub.add_parser('missing-policy-blocks')
    missing_policy.add_argument('--plan', required=True)
    missing_policy.add_argument('--blocks', required=True)
    missing_policy.add_argument('--raw', required=True)
    inspect_policy = sub.add_parser('inspect-policy')
    inspect_policy.add_argument('--plan', required=True)
    inspect_policy.add_argument('--raw', required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--config', required=True)
    analyze.add_argument('--migration-plan', required=True)
    analyze.add_argument('--migration-raw', required=True)
    analyze.add_argument('--policy-plan', required=True)
    analyze.add_argument('--policy-raw', required=True)
    analyze.add_argument('--component-costs', required=True)
    analyze.add_argument('--output', required=True)
    validate = sub.add_parser('validate')
    validate.add_argument('--results', required=True)
    manifest = sub.add_parser('verify-manifest')
    manifest.add_argument('--results', required=True)
    return parser

def main(argv: list[str] | None=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'info':
            _print({'experiment': 'E6+E9', 'python': platform.python_version(), 'python_supported': sys.version_info >= (3, 10), 'openssl_ed25519': openssl_info(), 'system': system_metadata(), 'cpu_pinning_probe': pin_to_first_available_cpu()})
            return 0
        if args.command == 'plan':
            _print(make_plans(args.config, args.output))
            return 0
        if args.command == 'plan-count':
            _print(count_plan(args.config))
            return 0
        if args.command == 'build-dataset':
            _print(build_dataset_index(dataset_plan=args.plan, dataset_dir=args.datasets, index=args.index, force=args.force))
            return 0
        if args.command == 'validate-datasets':
            result = validate_all_datasets(dataset_plan=args.plan, dataset_dir=args.datasets, verify_hashes=not args.skip_hashes)
            _print(result)
            return 0 if result['status'] == 'PASS' else 2
        if args.command == 'run-migration-block':
            _print(run_migration_block(migration_plan=args.plan, block_plan=args.blocks, block_index=args.block_index, dataset_dir=args.datasets, raw_dir=args.raw, work_root=args.work_root, overwrite=args.overwrite))
            return 0
        if args.command == 'missing-migration-blocks':
            print(','.join(map(str, missing_block_indices(plan_path=args.plan, block_plan=args.blocks, raw_dir=args.raw, kind='migration'))))
            return 0
        if args.command == 'inspect-migration':
            audit = inspect_raw(plan_path=args.plan, raw_dir=args.raw, kind='migration')
            audit.pop('results', None)
            _print(audit)
            return 0 if audit['complete'] else 2
        if args.command == 'analyze-migration':
            _print(analyze_migration(args.plan, args.raw, args.output, preferred_backend=args.preferred_backend))
            return 0
        if args.command == 'write-reference-catalog':
            write_reference_catalog(args.output, _csv_ints(args.n_values))
            _print(validate_cost_catalog(load_cost_catalog(args.output), required_n=_csv_ints(args.n_values)))
            return 0
        if args.command == 'build-component-catalog':
            _print(build_catalog_from_prior_results(e1_results=args.e1_results, e2_results=args.e2_results, e3_results=args.e3_results, availability_csv=args.availability_csv, output=args.output, source_revision=args.source_revision, deadline_ms=args.deadline_ms))
            return 0
        if args.command == 'validate-component-catalog':
            result = validate_cost_catalog(load_cost_catalog(args.catalog), required_n=_csv_ints(args.n_values), require_publication_eligible=args.require_publication)
            _print(result)
            return 0 if result['status'] == 'PASS' else 2
        if args.command == 'run-policy-block':
            _print(run_policy_block(policy_plan=args.plan, block_plan=args.blocks, block_index=args.block_index, component_costs=args.component_costs, migration_catalog=args.migration_catalog, raw_dir=args.raw, overwrite=args.overwrite))
            return 0
        if args.command == 'missing-policy-blocks':
            print(','.join(map(str, missing_block_indices(plan_path=args.plan, block_plan=args.blocks, raw_dir=args.raw, kind='policy'))))
            return 0
        if args.command == 'inspect-policy':
            audit = inspect_raw(plan_path=args.plan, raw_dir=args.raw, kind='policy')
            audit.pop('results', None)
            _print(audit)
            return 0 if audit['complete'] else 2
        if args.command == 'analyze':
            result = analyze_all(config_path=args.config, migration_plan=args.migration_plan, migration_raw=args.migration_raw, policy_plan=args.policy_plan, policy_raw=args.policy_raw, component_costs=args.component_costs, output_dir=args.output)
            _print(result)
            return 0 if result['status'] == 'PASS' else 2
        if args.command == 'validate':
            result = validate_results(args.results)
            _print(result)
            return 0 if result['status'] == 'PASS' else 2
        if args.command == 'verify-manifest':
            result = verify_manifest(args.results)
            _print(result)
            return 0 if result['status'] == 'PASS' else 2
        raise RuntimeError(f'unhandled command: {args.command}')
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        if os.environ.get('E6_DEBUG', '0') == '1':
            raise
        return 2
