from __future__ import annotations
import csv
import hashlib
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from .catalog import ModeCost, load_cost_catalog
from .util import stable_seed, system_metadata, sha256_file
MODES = ('full', 'leaf', 'ext')
MODE_INDEX = {mode: index for index, mode in enumerate(MODES)}

@dataclass(frozen=True)
class EpochItem:
    item_id: int
    shard: int
    epoch: int
    age_rank: int
    n: int
    costs: dict[str, ModeCost]

@dataclass(frozen=True)
class MigrationCost:
    transition: str
    n: int
    wall_ns: float
    bytes_read: float
    bytes_written: float

def _normalise(values: Sequence[float]) -> list[float]:
    total = sum((max(0.0, float(value)) for value in values))
    if total <= 0:
        return [1.0 / len(values)] * len(values)
    return [max(0.0, float(value)) / total for value in values]

def _zipf_weights(count: int, alpha: float, permutation: Sequence[int]) -> list[float]:
    ranked = [1.0 / (rank + 1) ** alpha for rank in range(count)]
    output = [0.0] * count
    for rank, item_index in enumerate(permutation):
        output[item_index] = ranked[rank]
    return _normalise(output)

def generate_workload_windows(*, count: int, workload: str, prediction_error: float, seed: int) -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(seed)
    permutation = list(range(count))
    rng.shuffle(permutation)
    if workload == 'uniform':
        previous = [1.0] * count
        actual = [1.0] * count
    elif workload == 'zipf0.8':
        previous = _zipf_weights(count, 0.8, permutation)
        actual = list(previous)
    elif workload == 'zipf1.2':
        previous = _zipf_weights(count, 1.2, permutation)
        actual = list(previous)
    elif workload == 'recency':
        half_life = max(4.0, count / 8.0)
        previous = [math.exp(-math.log(2.0) * age / half_life) for age in range(count)]
        actual = list(previous)
    elif workload == 'nonstationary':
        quarter = max(1, count // 4)
        first_hot = set(permutation[:quarter])
        second_hot = set(permutation[quarter:2 * quarter])
        previous = [8.0 if index in first_hot else 1.0 for index in range(count)]
        actual = [8.0 if index in second_hot else 1.0 for index in range(count)]
    else:
        raise ValueError(f'unsupported workload: {workload}')
    previous = _normalise(previous)
    actual = _normalise(actual)
    if prediction_error <= 0:
        predicted = list(actual)
    else:
        mix = min(1.0, max(0.0, float(prediction_error)))
        if workload == 'nonstationary':
            noise = previous
        else:
            noise_order = list(range(count))
            rng.shuffle(noise_order)
            noise = [actual[index] for index in noise_order]
        predicted = _normalise([(1.0 - mix) * target + mix * mistaken for target, mistaken in zip(actual, noise)])
    return (previous, predicted, actual)

def make_epoch_items(*, epoch_count: int, shard_count: int, n_values: Sequence[int], catalog: dict[tuple[int, str], ModeCost], seed: int) -> list[EpochItem]:
    if epoch_count < 1 or shard_count < 1:
        raise ValueError('epoch and shard counts must be positive')
    rng = random.Random(seed)
    values = [int(value) for value in n_values]
    items: list[EpochItem] = []
    for item_id in range(epoch_count):
        n = values[rng.randrange(len(values))]
        costs = {mode: catalog[n, mode] for mode in MODES}
        items.append(EpochItem(item_id=item_id, shard=item_id % shard_count, epoch=item_id // shard_count, age_rank=item_id, n=n, costs=costs))
    return items

def normalisation(items: Sequence[EpochItem]) -> dict[str, float]:
    all_full_storage = float(sum((item.costs['full'].storage_bytes for item in items)))
    return {'all_full_storage_bytes': all_full_storage, 'communication_max_bytes': max((cost.response_bytes for item in items for cost in item.costs.values())), 'cpu_max_ns': max((cost.cpu_ns for item in items for cost in item.costs.values())), 'energy_max_joules': max([cost.energy_joules or 0.0 for item in items for cost in item.costs.values()] or [0.0])}

def item_mode_objective(item: EpochItem, mode: str, query_weight: float, profile: dict[str, float], norms: dict[str, float]) -> float:
    cost = item.costs[mode]
    alpha = float(profile['alpha'])
    beta = float(profile['beta'])
    gamma = float(profile['gamma'])
    delta = float(profile['delta'])
    return alpha * cost.storage_bytes / norms['all_full_storage_bytes'] + beta * query_weight * cost.response_bytes / norms['communication_max_bytes'] + gamma * query_weight * cost.cpu_ns / norms['cpu_max_ns'] + delta * query_weight * (1.0 - cost.success_probability)

def assignment_objective(items: Sequence[EpochItem], assignment: Sequence[str], weights: Sequence[float], profile: dict[str, float], norms: dict[str, float]) -> float:
    return sum((item_mode_objective(item, mode, weight, profile, norms) for item, mode, weight in zip(items, assignment, weights)))

def assignment_storage(items: Sequence[EpochItem], assignment: Sequence[str]) -> int:
    return sum((item.costs[mode].storage_bytes for item, mode in zip(items, assignment)))

def exact_multiple_choice_dp(*, items: Sequence[EpochItem], weights: Sequence[float], profile: dict[str, float], norms: dict[str, float], budget_bytes: int, storage_quantum_bytes: int, current_assignment: Sequence[str] | None=None, migration_catalog: dict[tuple[int, str], MigrationCost] | None=None, evaluation_queries: int=1) -> tuple[list[str], float, dict[str, Any]]:
    quantum = int(storage_quantum_bytes)
    if quantum <= 0:
        raise ValueError('storage quantum must be positive')
    budget_units = budget_bytes // quantum
    storage_units: list[dict[str, int]] = []
    for item in items:
        row: dict[str, int] = {}
        for mode in MODES:
            value = item.costs[mode].storage_bytes
            if value % quantum:
                raise ValueError(f'storage cost {value} for n={item.n}, mode={mode} is not aligned to quantum {quantum}')
            row[mode] = value // quantum
        storage_units.append(row)
    nodes: list[tuple[int, int]] = [(-1, -1)]
    frontier: dict[int, tuple[float, int]] = {0: (0.0, 0)}
    max_frontier = 1
    for index, item in enumerate(items):
        candidates: dict[int, tuple[float, int, int, int]] = {}
        for used, (base_cost, previous_node) in frontier.items():
            for mode_index, mode in enumerate(MODES):
                new_used = used + storage_units[index][mode]
                if new_used > budget_units:
                    continue
                value = base_cost + item_mode_objective(item, mode, weights[index], profile, norms)
                if current_assignment is not None and migration_catalog is not None:
                    value += migration_item_penalty(item=item, old_mode=current_assignment[index], new_mode=mode, catalog=migration_catalog, profile=profile, norms=norms, evaluation_queries=evaluation_queries)
                incumbent = candidates.get(new_used)
                if incumbent is None or value < incumbent[0] - 1e-15:
                    candidates[new_used] = (value, previous_node, mode_index, used)
        pruned: dict[int, tuple[float, int]] = {}
        best_cost = math.inf
        for used in sorted(candidates):
            value, previous_node, mode_index, _previous_used = candidates[used]
            if value < best_cost - 1e-15:
                nodes.append((previous_node, mode_index))
                pruned[used] = (value, len(nodes) - 1)
                best_cost = value
        if not pruned:
            raise RuntimeError(f'no feasible DP state after item {index}')
        frontier = pruned
        max_frontier = max(max_frontier, len(frontier))
    best_used, (best_cost, node) = min(frontier.items(), key=lambda pair: pair[1][0])
    reversed_modes: list[str] = []
    while node != 0:
        previous, mode_index = nodes[node]
        reversed_modes.append(MODES[mode_index])
        node = previous
    if len(reversed_modes) != len(items):
        raise RuntimeError('DP back-pointer length mismatch')
    assignment = list(reversed(reversed_modes))
    return (assignment, best_cost, {'budget_units': budget_units, 'used_units': best_used, 'storage_quantum_bytes': quantum, 'max_frontier_states': max_frontier, 'nodes_created': len(nodes), 'exact_for_aligned_storage_costs': True})

def greedy_ratio_assignment(*, items: Sequence[EpochItem], weights: Sequence[float], profile: dict[str, float], norms: dict[str, float], budget_bytes: int, current_assignment: Sequence[str] | None=None, migration_catalog: dict[tuple[int, str], MigrationCost] | None=None, evaluation_queries: int=1) -> list[str]:
    assignment = ['ext'] * len(items)
    used = 0
    while True:
        best: tuple[float, float, int, str, int] | None = None
        for index, item in enumerate(items):
            current = assignment[index]
            current_cost = item_mode_objective(item, current, weights[index], profile, norms)
            if current_assignment is not None and migration_catalog is not None:
                current_cost += migration_item_penalty(item=item, old_mode=current_assignment[index], new_mode=current, catalog=migration_catalog, profile=profile, norms=norms, evaluation_queries=evaluation_queries)
            for target in MODES:
                additional = item.costs[target].storage_bytes - item.costs[current].storage_bytes
                if additional <= 0 or used + additional > budget_bytes:
                    continue
                target_cost = item_mode_objective(item, target, weights[index], profile, norms)
                if current_assignment is not None and migration_catalog is not None:
                    target_cost += migration_item_penalty(item=item, old_mode=current_assignment[index], new_mode=target, catalog=migration_catalog, profile=profile, norms=norms, evaluation_queries=evaluation_queries)
                benefit = current_cost - target_cost
                if benefit <= 0:
                    continue
                ratio = benefit / additional
                candidate = (ratio, benefit, -index, target, additional)
                if best is None or candidate > best:
                    best = candidate
        if best is None:
            break
        _ratio, _benefit, negative_index, target, additional = best
        index = -negative_index
        assignment[index] = target
        used += additional
    return assignment

def ranked_tier_assignment(*, items: Sequence[EpochItem], weights: Sequence[float], profile: dict[str, float], norms: dict[str, float], budget_bytes: int, ranking: str) -> list[str]:
    assignment = ['ext'] * len(items)
    if ranking == 'age':
        order = sorted(range(len(items)), key=lambda index: items[index].age_rank)
    elif ranking == 'popularity':
        order = sorted(range(len(items)), key=lambda index: (-weights[index], index))
    else:
        raise ValueError(ranking)
    used = 0
    for index in order:
        item = items[index]
        candidates = sorted(('leaf', 'full'), key=lambda mode: item_mode_objective(item, mode, weights[index], profile, norms))
        for mode in candidates:
            extra = item.costs[mode].storage_bytes
            if used + extra <= budget_bytes:
                assignment[index] = mode
                used += extra
                break
    for index in order:
        if assignment[index] != 'leaf':
            continue
        item = items[index]
        extra = item.costs['full'].storage_bytes - item.costs['leaf'].storage_bytes
        benefit = item_mode_objective(item, 'leaf', weights[index], profile, norms) - item_mode_objective(item, 'full', weights[index], profile, norms)
        if benefit > 0 and used + extra <= budget_bytes:
            assignment[index] = 'full'
            used += extra
    return assignment

def fixed_assignment(mode: str, count: int) -> list[str]:
    if mode not in MODES:
        raise ValueError(mode)
    return [mode] * count

def load_migration_catalog(path: str | Path) -> dict[tuple[int, str], MigrationCost]:
    result: dict[tuple[int, str], MigrationCost] = {}
    with Path(path).open(encoding='utf-8', newline='') as handle:
        for row in csv.DictReader(handle):
            n = int(row['n'])
            transition = str(row['transition'])
            result[n, transition] = MigrationCost(transition=transition, n=n, wall_ns=float(row['wall_ns_median']), bytes_read=float(row['bytes_read_median']), bytes_written=float(row['bytes_written_median']))
    if not result:
        raise ValueError('empty migration cost catalog')
    return result

def _nearest_migration_cost(catalog: dict[tuple[int, str], MigrationCost], n: int, transition: str) -> MigrationCost:
    direct = catalog.get((n, transition))
    if direct is not None:
        return direct
    candidates = [value for (candidate_n, candidate_transition), value in catalog.items() if candidate_transition == transition]
    if not candidates:
        raise ValueError(f'missing migration transition {transition}')
    return min(candidates, key=lambda value: abs(math.log2(value.n) - math.log2(n)))

def migration_item_penalty(*, item: EpochItem, old_mode: str, new_mode: str, catalog: dict[tuple[int, str], MigrationCost], profile: dict[str, float], norms: dict[str, float], evaluation_queries: int) -> float:
    if old_mode == new_mode:
        return 0.0
    value = _nearest_migration_cost(catalog, item.n, f'{old_mode}->{new_mode}')
    queries = max(1, int(evaluation_queries))
    return float(profile['gamma']) * value.wall_ns / (queries * norms['cpu_max_ns']) + float(profile['alpha']) * value.bytes_written / (queries * norms['all_full_storage_bytes'])

def migration_charge(*, items: Sequence[EpochItem], current: Sequence[str], target: Sequence[str], catalog: dict[tuple[int, str], MigrationCost], profile: dict[str, float], norms: dict[str, float], evaluation_queries: int) -> dict[str, float | int]:
    count = 0
    wall_ns = 0.0
    bytes_read = 0.0
    bytes_written = 0.0
    for item, old, new in zip(items, current, target):
        if old == new:
            continue
        value = _nearest_migration_cost(catalog, item.n, f'{old}->{new}')
        count += 1
        wall_ns += value.wall_ns
        bytes_read += value.bytes_read
        bytes_written += value.bytes_written
    queries = max(1, int(evaluation_queries))
    penalty = float(profile['gamma']) * wall_ns / (queries * norms['cpu_max_ns']) + float(profile['alpha']) * bytes_written / (queries * norms['all_full_storage_bytes'])
    return {'migration_count': count, 'migration_wall_ns': wall_ns, 'migration_bytes_read': bytes_read, 'migration_bytes_written': bytes_written, 'migration_penalty': penalty}

def mode_counts(assignment: Sequence[str]) -> dict[str, int]:
    return {mode: sum((1 for value in assignment if value == mode)) for mode in MODES}

def evaluate_assignment(*, algorithm: str, items: Sequence[EpochItem], assignment: Sequence[str], current_assignment: Sequence[str], predicted_weights: Sequence[float], actual_weights: Sequence[float], profile: dict[str, float], norms: dict[str, float], budget_bytes: int, migration_catalog: dict[tuple[int, str], MigrationCost], evaluation_queries: int, solver_metadata: dict[str, Any] | None=None) -> dict[str, Any]:
    storage = assignment_storage(items, assignment)
    feasible = storage <= budget_bytes
    planned = assignment_objective(items, assignment, predicted_weights, profile, norms)
    realised = assignment_objective(items, assignment, actual_weights, profile, norms)
    charge = migration_charge(items=items, current=current_assignment, target=assignment, catalog=migration_catalog, profile=profile, norms=norms, evaluation_queries=evaluation_queries)
    expected_bytes = sum((weight * item.costs[mode].response_bytes for item, mode, weight in zip(items, assignment, actual_weights)))
    expected_cpu = sum((weight * item.costs[mode].cpu_ns for item, mode, weight in zip(items, assignment, actual_weights)))
    energy_values = [item.costs[mode].energy_joules for item, mode in zip(items, assignment)]
    energy_available = all((value is not None for value in energy_values))
    expected_energy = sum((weight * float(item.costs[mode].energy_joules) for item, mode, weight in zip(items, assignment, actual_weights))) if energy_available else None
    completion_probability = sum((weight * item.costs[mode].success_probability for item, mode, weight in zip(items, assignment, actual_weights)))
    current_realised = assignment_objective(items, current_assignment, actual_weights, profile, norms)
    steady_benefit = current_realised - realised
    one_time_penalty_total = float(charge['migration_penalty']) * max(1, evaluation_queries)
    break_even_queries = one_time_penalty_total / steady_benefit if steady_benefit > 0 and one_time_penalty_total > 0 else 0.0 if one_time_penalty_total == 0 else None
    return {'algorithm': algorithm, 'feasible': feasible, 'storage_bytes': storage, 'storage_budget_bytes': budget_bytes, 'budget_violation_bytes': max(0, storage - budget_bytes), 'planned_steady_objective': planned, 'realised_steady_objective': realised, 'planned_amortised_objective': planned + float(charge['migration_penalty']), 'realised_amortised_objective': realised + float(charge['migration_penalty']), 'prediction_shift': realised - planned, 'expected_response_bytes_per_query': expected_bytes, 'expected_cpu_ns_per_query': expected_cpu, 'expected_energy_joules_per_query': expected_energy, 'completion_probability': completion_probability, 'availability_penalty': 1.0 - completion_probability, 'break_even_queries': break_even_queries, **charge, **{f'mode_{mode}_count': count for mode, count in mode_counts(assignment).items()}, 'assignment_digest': hashlib.sha256('|'.join(assignment).encode()).hexdigest(), 'solver_metadata': solver_metadata or {}}

def run_policy_case(case: dict[str, Any], *, component_costs_path: Path, migration_catalog_path: Path) -> dict[str, Any]:
    catalog = load_cost_catalog(component_costs_path)
    required_n = [int(value) for value in case['n_values']]
    for n in required_n:
        for mode in MODES:
            if (n, mode) not in catalog:
                raise ValueError(f'component cost catalog lacks n={n}, mode={mode}')
    migration_catalog = load_migration_catalog(migration_catalog_path)
    profile = {key: float(case['profile_weights'][key]) for key in ('alpha', 'beta', 'gamma', 'delta')}
    replicate_results: list[dict[str, Any]] = []
    for replicate in range(int(case['trace_replicates'])):
        seed = stable_seed(case['seed'], replicate, case['case_id'])
        items = make_epoch_items(epoch_count=int(case['epoch_count']), shard_count=int(case['shard_count']), n_values=required_n, catalog=catalog, seed=seed)
        previous, predicted, actual = generate_workload_windows(count=len(items), workload=str(case['workload']), prediction_error=float(case['prediction_error']), seed=seed ^ 58889)
        norms = normalisation(items)
        budget_bytes = int(math.floor(float(case['budget_fraction']) * norms['all_full_storage_bytes']))
        quantum = int(case['storage_quantum_bytes'])
        budget_bytes = budget_bytes // quantum * quantum
        if bool(case['run_dp']):
            current_assignment, _current_cost, _current_meta = exact_multiple_choice_dp(items=items, weights=previous, profile=profile, norms=norms, budget_bytes=budget_bytes, storage_quantum_bytes=quantum)
        else:
            current_assignment = greedy_ratio_assignment(items=items, weights=previous, profile=profile, norms=norms, budget_bytes=budget_bytes)
        assignments: dict[str, tuple[list[str], dict[str, Any]]] = {}
        if bool(case['run_dp']):
            dp_assignment, _dp_cost, dp_meta = exact_multiple_choice_dp(items=items, weights=predicted, profile=profile, norms=norms, budget_bytes=budget_bytes, storage_quantum_bytes=quantum, current_assignment=current_assignment, migration_catalog=migration_catalog, evaluation_queries=int(case['evaluation_queries']))
            assignments['dynamic_programming'] = (dp_assignment, dp_meta)
        assignments['greedy_ratio'] = (greedy_ratio_assignment(items=items, weights=predicted, profile=profile, norms=norms, budget_bytes=budget_bytes, current_assignment=current_assignment, migration_catalog=migration_catalog, evaluation_queries=int(case['evaluation_queries'])), {})
        assignments['all_full'] = (fixed_assignment('full', len(items)), {})
        assignments['all_leaf'] = (fixed_assignment('leaf', len(items)), {})
        assignments['all_ext'] = (fixed_assignment('ext', len(items)), {})
        assignments['age_tier'] = (ranked_tier_assignment(items=items, weights=predicted, profile=profile, norms=norms, budget_bytes=budget_bytes, ranking='age'), {})
        assignments['popularity_tier'] = (ranked_tier_assignment(items=items, weights=predicted, profile=profile, norms=norms, budget_bytes=budget_bytes, ranking='popularity'), {})
        rows: list[dict[str, Any]] = []
        for algorithm, (assignment, solver_metadata) in assignments.items():
            row = evaluate_assignment(algorithm=algorithm, items=items, assignment=assignment, current_assignment=current_assignment, predicted_weights=predicted, actual_weights=actual, profile=profile, norms=norms, budget_bytes=budget_bytes, migration_catalog=migration_catalog, evaluation_queries=int(case['evaluation_queries']), solver_metadata=solver_metadata)
            row.update({'replicate': replicate, 'seed': seed, 'normalisation': norms, 'current_assignment_digest': hashlib.sha256('|'.join(current_assignment).encode()).hexdigest()})
            rows.append(row)
        feasible_planned = [row for row in rows if row['feasible']]
        optimum = min((row['planned_amortised_objective'] for row in feasible_planned))
        if bool(case['run_dp']):
            oracle_assignment, oracle_value, _oracle_meta = exact_multiple_choice_dp(items=items, weights=actual, profile=profile, norms=norms, budget_bytes=budget_bytes, storage_quantum_bytes=quantum, current_assignment=current_assignment, migration_catalog=migration_catalog, evaluation_queries=int(case['evaluation_queries']))
        else:
            oracle_assignment = greedy_ratio_assignment(items=items, weights=actual, profile=profile, norms=norms, budget_bytes=budget_bytes, current_assignment=current_assignment, migration_catalog=migration_catalog, evaluation_queries=int(case['evaluation_queries']))
            oracle_value = assignment_objective(items, oracle_assignment, actual, profile, norms) + float(migration_charge(items=items, current=current_assignment, target=oracle_assignment, catalog=migration_catalog, profile=profile, norms=norms, evaluation_queries=int(case['evaluation_queries']))['migration_penalty'])
        for row in rows:
            row['planned_optimality_gap'] = (row['planned_amortised_objective'] - optimum) / max(abs(optimum), 1e-15) if row['feasible'] else None
            row['realised_oracle_objective'] = oracle_value
            row['realised_oracle_gap'] = (row['realised_amortised_objective'] - oracle_value) / max(abs(oracle_value), 1e-15) if row['feasible'] else None
            row['oracle_assignment_digest'] = hashlib.sha256('|'.join(oracle_assignment).encode()).hexdigest()
        replicate_results.extend(rows)
    checks = {'all_budget_violations_reported': all(((row['budget_violation_bytes'] == 0) == bool(row['feasible']) for row in replicate_results)), 'all_assignments_accounted': len(replicate_results) == int(case['trace_replicates']) * (7 if case['run_dp'] else 6), 'component_catalog_loaded': bool(catalog), 'migration_catalog_loaded': bool(migration_catalog)}
    return {'schema_version': 1, 'experiment': 'E9', 'case_id': case['case_id'], 'case': case, 'trial_kind': 'policy', 'component_catalog_publication_eligible': all((cost.publication_eligible for cost in catalog.values())), 'component_catalog_sha256': sha256_file(component_costs_path), 'migration_catalog_sha256': sha256_file(migration_catalog_path), 'results': replicate_results, 'checks': checks, 'all_checks_pass': all(checks.values()), 'system': system_metadata()}
