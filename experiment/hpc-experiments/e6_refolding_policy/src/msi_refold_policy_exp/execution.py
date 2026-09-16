from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable
from .dataset import DatasetSpec, build_dataset, validate_dataset
from .migration import run_migration_case
from .policy import run_policy_case
from .util import atomic_write_json_gz, jsonl_at, jsonl_iter, read_json_gz, pin_to_first_available_cpu

def result_path(raw_dir: str | Path, kind: str, case_id: str) -> Path:
    return Path(raw_dir) / f'{kind}-{case_id}.json.gz'

def build_dataset_index(*, dataset_plan: str | Path, dataset_dir: str | Path, index: int, force: bool=False) -> dict[str, Any]:
    row = jsonl_at(dataset_plan, int(index))
    spec = DatasetSpec.from_dict(row)
    return build_dataset(dataset_dir, spec, force=force)

def validate_all_datasets(*, dataset_plan: str | Path, dataset_dir: str | Path, verify_hashes: bool=True) -> dict[str, Any]:
    rows = []
    for index, row in enumerate(jsonl_iter(dataset_plan)):
        spec = DatasetSpec.from_dict(row)
        try:
            audit = validate_dataset(Path(dataset_dir) / spec.dataset_id, spec, verify_hashes=verify_hashes)
            rows.append({'index': index, 'dataset_id': spec.dataset_id, 'status': 'PASS', 'audit': audit})
        except Exception as exc:
            rows.append({'index': index, 'dataset_id': spec.dataset_id, 'status': 'FAIL', 'error': f'{type(exc).__name__}: {exc}'})
    return {'status': 'PASS' if rows and all((row['status'] == 'PASS' for row in rows)) else 'FAIL', 'datasets': len(rows), 'rows': rows}

def _load_cases_for_block(*, case_plan: str | Path, block_plan: str | Path, block_index: int) -> list[dict[str, Any]]:
    block = jsonl_at(block_plan, int(block_index))
    return [jsonl_at(case_plan, int(index)) for index in block['case_indices']]

def run_migration_block(*, migration_plan: str | Path, block_plan: str | Path, block_index: int, dataset_dir: str | Path, raw_dir: str | Path, work_root: str | Path, overwrite: bool=False) -> dict[str, Any]:
    raw = Path(raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    if __import__('os').environ.get('E6_DISABLE_PINNING', '0') != '1':
        pin_to_first_available_cpu()
    cases = _load_cases_for_block(case_plan=migration_plan, block_plan=block_plan, block_index=block_index)
    completed = []
    skipped = []
    for case in cases:
        target = result_path(raw, 'migration', case['case_id'])
        if target.exists() and (not overwrite):
            try:
                existing = read_json_gz(target)
                if existing.get('case_id') == case['case_id'] and existing.get('all_checks_pass'):
                    skipped.append(case['case_id'])
                    continue
            except Exception:
                pass
        result = run_migration_case(case, dataset_dir=Path(dataset_dir), work_root=Path(work_root))
        atomic_write_json_gz(target, result)
        completed.append(case['case_id'])
    return {'status': 'PASS', 'block_index': int(block_index), 'completed': completed, 'skipped': skipped, 'cases': len(cases)}

def run_policy_block(*, policy_plan: str | Path, block_plan: str | Path, block_index: int, component_costs: str | Path, migration_catalog: str | Path, raw_dir: str | Path, overwrite: bool=False) -> dict[str, Any]:
    raw = Path(raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    if __import__('os').environ.get('E6_DISABLE_PINNING', '0') != '1':
        pin_to_first_available_cpu()
    cases = _load_cases_for_block(case_plan=policy_plan, block_plan=block_plan, block_index=block_index)
    completed = []
    skipped = []
    for case in cases:
        target = result_path(raw, 'policy', case['case_id'])
        if target.exists() and (not overwrite):
            try:
                existing = read_json_gz(target)
                if existing.get('case_id') == case['case_id'] and existing.get('all_checks_pass'):
                    skipped.append(case['case_id'])
                    continue
            except Exception:
                pass
        result = run_policy_case(case, component_costs_path=Path(component_costs), migration_catalog_path=Path(migration_catalog))
        atomic_write_json_gz(target, result)
        completed.append(case['case_id'])
    return {'status': 'PASS', 'block_index': int(block_index), 'completed': completed, 'skipped': skipped, 'cases': len(cases)}

def inspect_raw(*, plan_path: str | Path, raw_dir: str | Path, kind: str) -> dict[str, Any]:
    expected = list(jsonl_iter(plan_path))
    raw = Path(raw_dir)
    valid: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    missing: list[int] = []
    corrupt: list[dict[str, Any]] = []
    for index, case in enumerate(expected):
        path = result_path(raw, kind, case['case_id'])
        if not path.exists():
            missing.append(index)
            continue
        try:
            result = read_json_gz(path)
            if result.get('case_id') != case['case_id']:
                raise ValueError('case_id mismatch')
            if not result.get('all_checks_pass', False):
                raise ValueError('internal checks did not pass')
            valid.append((index, case, result))
        except Exception as exc:
            corrupt.append({'index': index, 'case_id': case['case_id'], 'path': str(path), 'error': f'{type(exc).__name__}: {exc}'})
    return {'kind': kind, 'expected_cases': len(expected), 'valid_cases': len(valid), 'missing_indices': missing, 'corrupt_cases': corrupt, 'complete': len(valid) == len(expected) and (not missing) and (not corrupt), 'results': valid}

def missing_block_indices(*, plan_path: str | Path, block_plan: str | Path, raw_dir: str | Path, kind: str) -> list[int]:
    audit = inspect_raw(plan_path=plan_path, raw_dir=raw_dir, kind=kind)
    bad_case_indices = set(audit['missing_indices']) | {int(row['index']) for row in audit['corrupt_cases']}
    missing_blocks = []
    for block in jsonl_iter(block_plan):
        if any((int(index) in bad_case_indices for index in block['case_indices'])):
            missing_blocks.append(int(block['block_index']))
    return missing_blocks
