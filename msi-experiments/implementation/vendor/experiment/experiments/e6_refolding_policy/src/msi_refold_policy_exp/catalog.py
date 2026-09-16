from __future__ import annotations
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from .merkle import next_power_of_two
from .util import csv_write
CATALOG_FIELDS = ['schema_version', 'n', 'n_prime', 'mode', 'storage_bytes', 'response_bytes', 'cpu_ns', 'energy_joules', 'success_probability', 'deadline_ms', 'storage_source', 'response_source', 'cpu_source', 'energy_source', 'availability_source', 'publication_eligible', 'source_revision']
SCHEME_TO_MODE = {'B2_MSI_Full': 'full', 'B3_MSI_Leaf': 'leaf', 'B4_MSI_Ext': 'ext', 'B2_full': 'full', 'B3_leaf': 'leaf', 'B4_ext': 'ext', 'B4_ext_coalesced': 'ext'}

@dataclass(frozen=True)
class ModeCost:
    n: int
    n_prime: int
    mode: str
    storage_bytes: int
    response_bytes: float
    cpu_ns: float
    energy_joules: float | None
    success_probability: float
    deadline_ms: float
    publication_eligible: bool
    provenance: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {'schema_version': 1, 'n': self.n, 'n_prime': self.n_prime, 'mode': self.mode, 'storage_bytes': self.storage_bytes, 'response_bytes': self.response_bytes, 'cpu_ns': self.cpu_ns, 'energy_joules': '' if self.energy_joules is None else self.energy_joules, 'success_probability': self.success_probability, 'deadline_ms': self.deadline_ms, 'publication_eligible': str(self.publication_eligible).lower(), **self.provenance}

def _parse_bool(value: object) -> bool:
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y'}

def load_cost_catalog(path: str | Path) -> dict[tuple[int, str], ModeCost]:
    rows: dict[tuple[int, str], ModeCost] = {}
    with Path(path).open(encoding='utf-8', newline='') as handle:
        for line_number, row in enumerate(csv.DictReader(handle), 2):
            try:
                n = int(row['n'])
                n_prime = int(row['n_prime'])
                mode = str(row['mode'])
                energy_raw = str(row.get('energy_joules', '')).strip()
                cost = ModeCost(n=n, n_prime=n_prime, mode=mode, storage_bytes=int(round(float(row['storage_bytes']))), response_bytes=float(row['response_bytes']), cpu_ns=float(row['cpu_ns']), energy_joules=float(energy_raw) if energy_raw else None, success_probability=float(row['success_probability']), deadline_ms=float(row.get('deadline_ms', 500.0)), publication_eligible=_parse_bool(row.get('publication_eligible', False)), provenance={'storage_source': str(row.get('storage_source', '')), 'response_source': str(row.get('response_source', '')), 'cpu_source': str(row.get('cpu_source', '')), 'energy_source': str(row.get('energy_source', '')), 'availability_source': str(row.get('availability_source', '')), 'source_revision': str(row.get('source_revision', ''))})
            except Exception as exc:
                raise ValueError(f'invalid component-cost row {line_number}: {exc}') from exc
            key = (n, mode)
            if key in rows:
                raise ValueError(f'duplicate cost row for n={n}, mode={mode}')
            rows[key] = cost
    validate_cost_catalog(rows)
    return rows

def validate_cost_catalog(rows: dict[tuple[int, str], ModeCost], *, required_n: Iterable[int] | None=None, require_publication_eligible: bool=False) -> dict[str, Any]:
    failures: list[str] = []
    modes = {'full', 'leaf', 'ext'}
    ns = sorted({n for n, _ in rows})
    for n in ns:
        if {mode for row_n, mode in rows if row_n == n} != modes:
            failures.append(f'n={n} does not contain exactly full/leaf/ext')
        for mode in modes:
            cost = rows.get((n, mode))
            if cost is None:
                continue
            if cost.n_prime != next_power_of_two(n):
                failures.append(f'n={n}, mode={mode}: incorrect n_prime')
            if cost.storage_bytes < 0 or cost.response_bytes <= 0 or cost.cpu_ns <= 0:
                failures.append(f'n={n}, mode={mode}: non-positive cost')
            if not 0.0 <= cost.success_probability <= 1.0:
                failures.append(f'n={n}, mode={mode}: invalid success probability')
            if mode == 'ext' and cost.storage_bytes != 0:
                failures.append(f'n={n}: ext auxiliary storage must be zero')
            if require_publication_eligible and (not cost.publication_eligible):
                failures.append(f'n={n}, mode={mode}: not publication eligible')
    for n in required_n or []:
        for mode in modes:
            if (int(n), mode) not in rows:
                failures.append(f'missing n={n}, mode={mode}')
    return {'status': 'PASS' if not failures else 'FAIL', 'rows': len(rows), 'epoch_lengths': ns, 'all_publication_eligible': bool(rows) and all((c.publication_eligible for c in rows.values())), 'failures': failures}

def write_reference_catalog(path: str | Path, epoch_lengths: Iterable[int]) -> None:
    rows: list[dict[str, Any]] = []
    for n in sorted(set((int(value) for value in epoch_lengths))):
        n_prime = next_power_of_two(n)
        depth = int(math.log2(n_prime))
        base_response = 2410.0
        path_witness = 5.0 + 33.0 * depth
        leaf_witness = 5.0 + 32.0 * n_prime
        raw_full = 64 + (2 * n_prime - 2) * 32
        raw_leaf = 64 + n_prime * 32
        allocation = 4096
        storage = {'full': int(math.ceil(raw_full / allocation) * allocation), 'leaf': int(math.ceil(raw_leaf / allocation) * allocation), 'ext': 0}
        response = {'full': base_response + path_witness, 'leaf': base_response + leaf_witness, 'ext': base_response + path_witness}
        cpu = {'full': 180000.0 + 5500.0 * (depth + 1), 'leaf': 180000.0 + 1450.0 * n_prime + 5500.0 * (depth + 1), 'ext': 190000.0 + 5500.0 * (depth + 1)}
        success = {'full': 0.995, 'leaf': 0.99, 'ext': 0.93}
        for mode in ('full', 'leaf', 'ext'):
            rows.append(ModeCost(n=n, n_prime=n_prime, mode=mode, storage_bytes=storage[mode], response_bytes=response[mode], cpu_ns=cpu[mode], energy_joules=cpu[mode] * 2.2e-10, success_probability=success[mode], deadline_ms=500.0, publication_eligible=False, provenance={'storage_source': 'self-contained-reference-fixture', 'response_source': 'self-contained-reference-fixture', 'cpu_source': 'self-contained-reference-fixture', 'energy_source': 'self-contained-reference-fixture', 'availability_source': 'synthetic-reference-only', 'source_revision': 'E6-reference-fixture-v1'}).as_dict())
    csv_write(path, rows, CATALOG_FIELDS)

def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))

def build_catalog_from_prior_results(*, e1_results: str | Path, e2_results: str | Path, e3_results: str | Path, availability_csv: str | Path, output: str | Path, source_revision: str, deadline_ms: float=500.0) -> dict[str, Any]:
    import statistics
    e1 = Path(e1_results)
    e2 = Path(e2_results)
    e3 = Path(e3_results)
    availability_path = Path(availability_csv)
    storage_samples: dict[tuple[int, str], list[int]] = {}
    theory_path = e1 / 'theory_vs_serialization.csv'
    for row in _read_csv(theory_path):
        try:
            n = int(row['n'])
            mode = str(row['mode'])
            epochs = int(row['epochs_total'])
        except (KeyError, ValueError):
            continue
        if mode not in {'full', 'leaf', 'ext'} or epochs <= 0:
            continue
        theoretical_total = float(row.get('theoretical_hash_bytes', 0) or 0)
        logical_per_epoch = theoretical_total / epochs
        if mode == 'ext':
            allocated_per_epoch = 0
        else:
            allocated_per_epoch = int(math.ceil(logical_per_epoch / 4096.0) * 4096)
        storage_samples.setdefault((n, mode), []).append(allocated_per_epoch)
    storage = {key: int(round(statistics.median(values))) for key, values in storage_samples.items()}
    cpu_samples: dict[tuple[int, str], list[float]] = {}
    energy_samples: dict[tuple[int, str], list[float]] = {}
    for row in _read_csv(e2 / 'latency_summary.csv'):
        if row.get('ablation') != 'safe':
            continue
        if int(float(row.get('block_bytes', 0) or 0)) != 2048:
            continue
        if row.get('layout') != 'epoch_packed' or row.get('codec') != 'raw-v1':
            continue
        mode = SCHEME_TO_MODE.get(str(row.get('scheme')))
        if mode is None:
            continue
        n = int(row['epoch_length'])
        cpu_samples.setdefault((n, mode), []).append(float(row['median_ns']))
    throughput_path = e2 / 'throughput_resources.csv'
    if throughput_path.exists():
        for row in _read_csv(throughput_path):
            if row.get('ablation') != 'safe':
                continue
            if int(float(row.get('block_bytes', 0) or 0)) != 2048:
                continue
            if row.get('layout') != 'epoch_packed' or row.get('codec') != 'raw-v1':
                continue
            mode = SCHEME_TO_MODE.get(str(row.get('scheme')))
            if mode is None:
                continue
            n = int(row['epoch_length'])
            raw = str(row.get('joules_per_query_mean_valid', '')).strip()
            if raw:
                energy_samples.setdefault((n, mode), []).append(float(raw))
    cpu = {key: statistics.median(values) for key, values in cpu_samples.items()}
    energy = {key: statistics.median(values) for key, values in energy_samples.items()}
    response_samples: dict[tuple[int, str], list[float]] = {}
    for row in _read_csv(e3 / 'wire_sizes.csv'):
        variant = str(row.get('variant'))
        if variant == 'B4_ext_split':
            continue
        if int(float(row.get('block_bytes', 0) or 0)) != 2048:
            continue
        mode = SCHEME_TO_MODE.get(variant) or SCHEME_TO_MODE.get(str(row.get('scheme')))
        if mode is None:
            continue
        n = int(row['epoch_length_n'])
        response_samples.setdefault((n, mode), []).append(float(row['total_response_bytes']))
    response = {key: statistics.median(values) for key, values in response_samples.items()}
    availability_samples: dict[tuple[int, str], list[float]] = {}
    availability_source: dict[tuple[int, str], set[str]] = {}
    for line_number, row in enumerate(_read_csv(availability_path), 2):
        n = int(row['n'])
        mode = str(row['mode'])
        if not _parse_bool(row.get('publication_eligible', False)):
            raise ValueError(f'availability row {line_number} is not publication eligible; synthetic or placeholder availability inputs are rejected')
        source = str(row.get('source', '')).strip()
        if not source:
            raise ValueError(f'availability row {line_number} lacks measurement provenance')
        probability = float(row['success_probability'])
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f'availability row {line_number} is outside [0,1]')
        availability_samples.setdefault((n, mode), []).append(probability)
        availability_source.setdefault((n, mode), set()).add(source)
    availability = {key: statistics.median(values) for key, values in availability_samples.items()}
    common = sorted(set(storage) & set(cpu) & set(response) & set(availability))
    rows: list[dict[str, Any]] = []
    for n, mode in common:
        rows.append(ModeCost(n=n, n_prime=next_power_of_two(n), mode=mode, storage_bytes=storage[n, mode], response_bytes=response[n, mode], cpu_ns=cpu[n, mode], energy_joules=energy.get((n, mode)), success_probability=availability[n, mode], deadline_ms=deadline_ms, publication_eligible=True, provenance={'storage_source': f'{theory_path}#verified-logical-bytes-rounded-to-4096', 'response_source': f"{e3 / 'wire_sizes.csv'}#block_bytes=2048,coalesced", 'cpu_source': f"{e2 / 'latency_summary.csv'}#safe,2048B,epoch_packed,raw-v1", 'energy_source': str(throughput_path), 'availability_source': ';'.join(sorted(availability_source[n, mode])), 'source_revision': source_revision}).as_dict())
    csv_write(output, rows, CATALOG_FIELDS)
    loaded = load_cost_catalog(output)
    audit = validate_cost_catalog(loaded, require_publication_eligible=True)
    if audit['status'] != 'PASS':
        raise ValueError(f"constructed catalog failed validation: {audit['failures']}")
    return audit
