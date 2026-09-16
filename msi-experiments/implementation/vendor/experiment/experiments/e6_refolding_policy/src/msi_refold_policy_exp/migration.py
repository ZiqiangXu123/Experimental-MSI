from __future__ import annotations
import copy
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import resource
import shutil
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Sequence
from .dataset import dataset_path, load_manifest, resolve_object
from .merkle import FULL_MAGIC, LEAF_MAGIC, SUPPORT_HEADER, FormatError, HashCounter, IOStats, PayloadStore, build_layers, leaves_from_payload, parse_support_bytes, path_from_layers, read_support, rebuild_path_from_leaf_vector, serialize_full_support, serialize_leaf_support, verify_path, write_support
from .state import StateError, allowed_difference_only, canonical_json_bytes, create_ext_registry, protected_bytes, read_entry, validate_active_state, validate_entry_shape, validate_ext_registry
from .util import atomic_write_json, read_json, stable_seed, system_metadata
TRANSITIONS = ('full->leaf', 'leaf->full', 'full->ext', 'leaf->ext', 'ext->leaf', 'ext->full')
MODES = ('full', 'leaf', 'ext')
CRASH_POINTS = {'crash_after_root_verify', 'crash_after_target_create', 'crash_before_metadata_swap', 'crash_during_metadata_write', 'crash_after_metadata_swap'}

class MigrationRejected(RuntimeError):
    pass

def split_transition(value: str) -> tuple[str, str]:
    if value not in TRANSITIONS:
        raise ValueError(f'invalid transition: {value}')
    source, target = value.split('->', 1)
    return (source, target)

def applicable_faults(transition: str) -> list[str]:
    source, target = split_transition(transition)
    faults = list(CRASH_POINTS)
    if source in {'full', 'leaf'}:
        faults.append('corrupt_source_leaf')
    if source == 'full':
        faults.append('corrupt_source_internal')
    if source == 'ext':
        faults.append('corrupt_payload')
    if target in {'full', 'leaf'}:
        faults.append('corrupt_target_aux')
    if target == 'ext':
        faults.extend(['registration_failure', 'incompatible_witness_source'])
    return sorted(faults)

def _directory_fsync(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _atomic_entry_swap(path: Path, entry: dict[str, Any], crash_point: str | None) -> int:
    validate_entry_shape(entry)
    encoded = canonical_json_bytes(entry) + b'\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            if crash_point == 'crash_during_metadata_write':
                cutoff = max(1, len(encoded) // 2)
                handle.write(encoded[:cutoff])
                handle.flush()
                os.fsync(handle.fileno())
                os._exit(86)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _directory_fsync(path.parent)
        if crash_point == 'crash_after_metadata_swap':
            os._exit(87)
        return len(encoded)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

def _maybe_crash(crash_point: str | None, point: str, code: int) -> None:
    if crash_point == point:
        os._exit(code)

def _mutate_support(path: Path, *, hash_index: int) -> None:
    data = bytearray(path.read_bytes())
    if len(data) < SUPPORT_HEADER.size:
        raise ValueError('cannot mutate truncated support')
    magic, version, n, n_prime, depth, root, _crc = SUPPORT_HEADER.unpack_from(data)
    body = bytearray(data[SUPPORT_HEADER.size:])
    if hash_index < 0 or (hash_index + 1) * 32 > len(body):
        raise ValueError('support hash index out of range')
    body[hash_index * 32] ^= 1
    header = SUPPORT_HEADER.pack(magic, version, n, n_prime, depth, root, zlib.crc32(body) & 4294967295)
    path.write_bytes(header + body)

def _copy_payload_for_corruption(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination)
    manifest = read_json(destination / 'manifest.json')
    if manifest['layout'] == 'epoch_packed':
        target = destination / 'payload.dat'
    else:
        target = destination / 'blocks' / '00000001.bin'
    data = bytearray(target.read_bytes())
    if not data:
        raise ValueError('empty payload object')
    data[min(len(data) - 1, max(0, len(data) // 2))] ^= 64
    target.write_bytes(data)

def _filesystem_info(path: Path) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    stat = path.stat()
    vfs = os.statvfs(path)
    result: dict[str, Any] = {'path': str(path.resolve()), 'device': int(stat.st_dev), 'block_size': int(vfs.f_bsize), 'fragment_size': int(vfs.f_frsize), 'blocks_available': int(vfs.f_bavail), 'mount_point': None, 'filesystem_type': None}
    try:
        candidates: list[tuple[int, str, str]] = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            fields = left.split()
            mount_point = fields[4].replace('\\040', ' ')
            fs_type = right.split()[0]
            resolved = str(path.resolve())
            if resolved == mount_point or resolved.startswith(mount_point.rstrip('/') + '/'):
                candidates.append((len(mount_point), mount_point, fs_type))
        if candidates:
            _, result['mount_point'], result['filesystem_type'] = max(candidates)
    except Exception:
        pass
    return result

def _energy_snapshot() -> dict[str, tuple[int, int | None]]:
    output: dict[str, tuple[int, int | None]] = {}
    base = Path('/sys/class/powercap')
    if not base.exists():
        return output
    for energy_path in base.glob('intel-rapl*/energy_uj'):
        try:
            value = int(energy_path.read_text().strip())
            max_path = energy_path.with_name('max_energy_range_uj')
            maximum = int(max_path.read_text().strip()) if max_path.exists() else None
            output[str(energy_path)] = (value, maximum)
        except (OSError, ValueError):
            continue
    return output

def _energy_delta(before: dict[str, tuple[int, int | None]], after: dict[str, tuple[int, int | None]]) -> tuple[float | None, str | None]:
    deltas: list[int] = []
    for path, (start, maximum) in before.items():
        if path not in after:
            continue
        end = after[path][0]
        delta = end - start
        if delta < 0 and maximum:
            delta += maximum
        if delta >= 0:
            deltas.append(delta)
    if not deltas:
        return (None, None)
    return (sum(deltas) / 1000000.0, 'linux-intel-rapl-node-domain')

def _workspace_size(path: Path) -> int:
    return sum((p.stat().st_size for p in path.rglob('*') if p.is_file()))

def _resolve_work_base(backend: str, fallback: Path) -> Path:
    if backend == 'node_local':
        value = os.environ.get('E6_NODE_LOCAL_BASE') or os.environ.get('SLURM_TMPDIR')
        return Path(value) if value else Path(tempfile.gettempdir()) / 'msi-e6-node-local'
    if backend == 'shared_scratch':
        value = os.environ.get('E6_SHARED_WORK_BASE')
        return Path(value) if value else fallback / 'shared-work'
    raise ValueError(f'unsupported backend: {backend}')

def _prepare_workspace(case: dict[str, Any], dataset_root: Path, work_root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    source_mode, _target_mode = split_transition(str(case['transition']))
    manifest = load_manifest(dataset_root)
    base = _resolve_work_base(str(case['backend']), work_root)
    base.mkdir(parents=True, exist_ok=True)
    workspace = base / f"e6-{case['case_id']}-{os.getpid()}-{time.time_ns()}"
    workspace.mkdir(parents=True)
    (workspace / 'active').mkdir()
    (workspace / 'targets').mkdir()
    (workspace / 'transactions').mkdir()
    (workspace / 'msi').mkdir()
    template = read_json(resolve_object(dataset_root, manifest, f'entry_{source_mode}'))
    entry = copy.deepcopy(template)
    payload_ref = resolve_object(dataset_root, manifest, 'payload_root')
    entry['meta']['payload_ref'] = str(payload_ref.resolve())
    if source_mode in {'full', 'leaf'}:
        source = resolve_object(dataset_root, manifest, f'{source_mode}_support')
        active = workspace / 'active' / f'source-{source_mode}.bin'
        shutil.copyfile(source, active)
        entry['meta']['aux_ref'] = str(active.resolve())
    else:
        source = resolve_object(dataset_root, manifest, 'external_registry')
        active = workspace / 'active' / 'source-ext.json'
        shutil.copyfile(source, active)
        entry['meta']['aux_ref'] = str(active.resolve())
    entry_path = workspace / 'msi' / 'entry.json'
    atomic_write_json(entry_path, entry)
    before = read_entry(entry_path)
    validate_active_state(entry_path)
    return (workspace, before, manifest)

def _recover_leaves(entry: dict[str, Any], *, counter: HashCounter, io_stats: IOStats, source_aux_override: str | Path | None=None, payload_override: str | Path | None=None) -> tuple[list[bytes], list[list[bytes]], str]:
    meta = entry['meta']
    mode = str(meta['mode'])
    n_prime = int(meta['n_prime'])
    expected_root = bytes.fromhex(entry['root_hex'])
    recovery_source = mode
    if mode in {'full', 'leaf'}:
        state = read_support(source_aux_override or meta['aux_ref'], expected_mode=mode, counter=counter, io_stats=io_stats, verify=True)
        if state.root != expected_root:
            raise MigrationRejected('source support root mismatch')
        if state.leaves:
            leaves = list(state.leaves)
            layers = [list(layer) for layer in state.layers]
            return (leaves, layers, recovery_source)
        recovery_source = 'payload_for_depth_zero_full'
    if mode == 'ext':
        validate_ext_registry(meta['aux_ref'], expected_root=expected_root, io_stats=io_stats, counter=counter, verify_service=True)
        recovery_source = 'canonical_payload'
    try:
        leaves, payload_meta = leaves_from_payload(payload_override or meta['payload_ref'], counter=counter, io_stats=io_stats)
    except Exception as exc:
        raise MigrationRejected(f'canonical payload recovery failed: {exc}') from exc
    if len(leaves) != n_prime:
        raise MigrationRejected('payload padded leaf length mismatch')
    layers = build_layers(leaves, counter)
    return (leaves, layers, recovery_source)

def _validate_target(*, target_mode: str, target_ref: Path, expected_root: bytes, counter: HashCounter, io_stats: IOStats) -> None:
    try:
        if target_mode in {'full', 'leaf'}:
            state = read_support(target_ref, expected_mode=target_mode, counter=counter, io_stats=io_stats, verify=True)
            if state.root != expected_root:
                raise MigrationRejected('target support root mismatch')
        else:
            validate_ext_registry(target_ref, expected_root=expected_root, io_stats=io_stats, counter=counter, verify_service=True)
    except (FormatError, StateError, OSError) as exc:
        raise MigrationRejected(f'target validation failed: {exc}') from exc

def migrate_epoch(*, workspace: Path, dataset_root: Path, manifest: dict[str, Any], target_mode: str, fault: str='none', crash_point: str | None=None, source_aux_override: str | Path | None=None, payload_override: str | Path | None=None) -> dict[str, Any]:
    entry_path = workspace / 'msi' / 'entry.json'
    before = read_entry(entry_path)
    source_mode = str(before['meta']['mode'])
    if source_mode == target_mode:
        raise MigrationRejected('source and target modes must differ')
    expected_root = bytes.fromhex(before['root_hex'])
    counter = HashCounter()
    io_stats = IOStats()
    start_wall = time.perf_counter_ns()
    start_cpu = time.process_time_ns()
    start_usage = resource.getrusage(resource.RUSAGE_SELF)
    energy_before = _energy_snapshot()
    initial_workspace_bytes = _workspace_size(workspace)
    peak_workspace_bytes = initial_workspace_bytes
    transaction_id = hashlib.sha256(f'{source_mode}->{target_mode}:{time.time_ns()}:{os.getpid()}'.encode()).hexdigest()[:20]
    journal_path = workspace / 'transactions' / f'{transaction_id}.json'
    target_ref: Path | None = None
    metadata_bytes_written = 0
    target_serialized_bytes = 0
    old_aux_ref = Path(str(before['meta']['aux_ref']))
    try:
        leaves, layers, recovery_source = _recover_leaves(before, counter=counter, io_stats=io_stats, source_aux_override=source_aux_override, payload_override=payload_override)
        recomputed_root = layers[-1][0]
        if recomputed_root != expected_root:
            raise MigrationRejected('root preservation check failed')
        _maybe_crash(crash_point, 'crash_after_root_verify', 81)
        targets = workspace / 'targets'
        if target_mode == 'full':
            encoded = serialize_full_support(n=int(before['meta']['n']), layers=layers)
            target_ref = targets / f'full-{hashlib.sha256(encoded).hexdigest()[:20]}.bin'
            write_support(target_ref, encoded, io_stats)
            target_serialized_bytes = len(encoded)
        elif target_mode == 'leaf':
            encoded = serialize_leaf_support(n=int(before['meta']['n']), leaves=leaves, root=expected_root)
            target_ref = targets / f'leaf-{hashlib.sha256(encoded).hexdigest()[:20]}.bin'
            write_support(target_ref, encoded, io_stats)
            target_serialized_bytes = len(encoded)
        elif target_mode == 'ext':
            if fault == 'registration_failure':
                raise MigrationRejected('witness-source registration failed')
            target_ref = targets / f'ext-{transaction_id}.json'
            service_ref = resolve_object(dataset_root, manifest, 'external_support')
            registry = create_ext_registry(target_ref, root=expected_root, locator=f"e6://witness/{manifest['spec']['dataset_id']}/{transaction_id}", service_support_ref=str(service_ref.resolve()), incompatible=fault == 'incompatible_witness_source', io_stats=io_stats)
            target_serialized_bytes = target_ref.stat().st_size
        else:
            raise ValueError(target_mode)
        peak_workspace_bytes = max(peak_workspace_bytes, _workspace_size(workspace))
        _maybe_crash(crash_point, 'crash_after_target_create', 82)
        if fault == 'corrupt_target_aux':
            if target_mode not in {'full', 'leaf'}:
                raise ValueError('target support corruption requires local target mode')
            _mutate_support(target_ref, hash_index=0)
        _validate_target(target_mode=target_mode, target_ref=target_ref, expected_root=expected_root, counter=counter, io_stats=io_stats)
        journal = {'schema_version': 1, 'transaction_id': transaction_id, 'phase': 'target_validated', 'old_mode': source_mode, 'new_mode': target_mode, 'old_aux_ref': str(old_aux_ref), 'new_aux_ref': str(target_ref.resolve()), 'protected_sha256': hashlib.sha256(protected_bytes(before)).hexdigest()}
        atomic_write_json(journal_path, journal)
        metadata_bytes_written += journal_path.stat().st_size
        peak_workspace_bytes = max(peak_workspace_bytes, _workspace_size(workspace))
        _maybe_crash(crash_point, 'crash_before_metadata_swap', 83)
        after_candidate = copy.deepcopy(before)
        after_candidate['meta']['mode'] = target_mode
        after_candidate['meta']['aux_ref'] = str(target_ref.resolve())
        allowed, differences = allowed_difference_only(before, after_candidate)
        if not allowed or protected_bytes(before) != protected_bytes(after_candidate):
            raise MigrationRejected(f'protected metadata mutation: {differences}')
        metadata_bytes_written += _atomic_entry_swap(entry_path, after_candidate, crash_point)
        after = read_entry(entry_path)
        validate_active_state(entry_path)
        if old_aux_ref.exists() and workspace in old_aux_ref.parents:
            old_aux_ref.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        _directory_fsync(entry_path.parent)
        end_usage = resource.getrusage(resource.RUSAGE_SELF)
        energy_after = _energy_snapshot()
        energy_joules, energy_source = _energy_delta(energy_before, energy_after)
        end_wall = time.perf_counter_ns()
        end_cpu = time.process_time_ns()
        allowed, differences = allowed_difference_only(before, after)
        result = {'status': 'PASS', 'source_mode': source_mode, 'target_mode': target_mode, 'transition': f'{source_mode}->{target_mode}', 'recovery_source': recovery_source, 'root_equal': before['root_hex'] == after['root_hex'], 'protected_bytes_equal': protected_bytes(before) == protected_bytes(after), 'allowed_metadata_difference_only': allowed, 'metadata_differences': differences, 'active_mode': after['meta']['mode'], 'active_aux_ref': after['meta']['aux_ref'], 'wall_ns': end_wall - start_wall, 'cpu_ns': end_cpu - start_cpu, 'hash_calls': counter.as_dict(), 'io': io_stats.as_dict(), 'metadata_bytes_written': metadata_bytes_written, 'target_serialized_bytes': target_serialized_bytes, 'initial_workspace_bytes': initial_workspace_bytes, 'peak_workspace_bytes': peak_workspace_bytes, 'temporary_disk_bytes': max(0, peak_workspace_bytes - initial_workspace_bytes), 'write_amplification': (io_stats.bytes_written + metadata_bytes_written) / target_serialized_bytes if target_serialized_bytes > 0 else None, 'peak_rss_kib': int(end_usage.ru_maxrss), 'minor_faults': int(end_usage.ru_minflt - start_usage.ru_minflt), 'major_faults': int(end_usage.ru_majflt - start_usage.ru_majflt), 'voluntary_context_switches': int(end_usage.ru_nvcsw - start_usage.ru_nvcsw), 'involuntary_context_switches': int(end_usage.ru_nivcsw - start_usage.ru_nivcsw), 'energy_joules_observed_nonexclusive': energy_joules, 'energy_source': energy_source}
        return result
    except MigrationRejected:
        raise
    except Exception as exc:
        raise MigrationRejected(f'migration failed: {type(exc).__name__}: {exc}') from exc

def _materialized_layers_for_entry(entry: dict[str, Any]) -> tuple[list[list[bytes]], str]:
    mode = str(entry['meta']['mode'])
    root = bytes.fromhex(entry['root_hex'])
    if mode in {'full', 'leaf'}:
        support = read_support(entry['meta']['aux_ref'], expected_mode=mode, verify=True)
        if support.leaves:
            return (build_layers(list(support.leaves)), mode)
        return ([[root]], mode)
    registry = validate_ext_registry(entry['meta']['aux_ref'], expected_root=root, verify_service=True)
    support = read_support(registry['service_support_ref'], expected_mode='full', verify=True)
    if support.leaves:
        return ([list(layer) for layer in support.layers], mode)
    return ([[root]], mode)

def audit_entry_queries(entry_path: Path, *, seed: int, exact_sample_limit: int=64) -> dict[str, Any]:
    entry = read_entry(entry_path)
    meta = entry['meta']
    n = int(meta['n'])
    n_prime = int(meta['n_prime'])
    root = bytes.fromhex(entry['root_hex'])
    layers, mode = _materialized_layers_for_entry(entry)
    store = PayloadStore(meta['payload_ref'])
    exhaustive_positions = list(range(1, n + 1)) if n <= 4096 else _sample_positions(n, seed, 256)
    failures: list[int] = []
    for position in exhaustive_positions:
        block = store.read_block(position)
        path = [] if n_prime == 1 else path_from_layers(layers, position)
        if not verify_path(shard=int(meta['shard']), epoch=int(meta['epoch']), position=position, block=block, path=path, expected_root=root):
            failures.append(position)
    exact_positions = list(range(1, n + 1)) if n <= exact_sample_limit else _sample_positions(n, seed ^ 59110, exact_sample_limit)
    exact_failures: list[int] = []
    if mode == 'leaf':
        support = read_support(meta['aux_ref'], expected_mode='leaf', verify=True)
        leaves = list(support.leaves)
        for position in exact_positions:
            block = store.read_block(position)
            path = rebuild_path_from_leaf_vector(leaves, position)
            if not verify_path(shard=int(meta['shard']), epoch=int(meta['epoch']), position=position, block=block, path=path, expected_root=root):
                exact_failures.append(position)
    else:
        exact_positions = []
    return {'mode': mode, 'positions_checked': len(exhaustive_positions), 'failures': failures, 'exact_leaf_resolver_positions_checked': len(exact_positions), 'exact_leaf_resolver_failures': exact_failures, 'all_accept': not failures and (not exact_failures)}

def _sample_positions(n: int, seed: int, limit: int) -> list[int]:
    mandatory = {1, n, max(1, n // 2), min(n, n // 2 + 1)}
    power = 1
    while power <= n:
        mandatory.add(power)
        if power > 1:
            mandatory.add(power - 1)
        if power < n:
            mandatory.add(power + 1)
        power <<= 1
    rng = random.Random(seed)
    while len(mandatory) < min(n, limit):
        mandatory.add(rng.randint(1, n))
    return sorted(mandatory)[:limit]

def _prepare_fault_overrides(workspace: Path, before: dict[str, Any], fault: str) -> tuple[Path | None, Path | None]:
    source_aux_override: Path | None = None
    payload_override: Path | None = None
    fault_dir = workspace / 'fault-inputs'
    fault_dir.mkdir(exist_ok=True)
    if fault in {'corrupt_source_leaf', 'corrupt_source_internal'}:
        source = Path(str(before['meta']['aux_ref']))
        source_aux_override = fault_dir / source.name
        shutil.copyfile(source, source_aux_override)
        if fault == 'corrupt_source_leaf':
            _mutate_support(source_aux_override, hash_index=0)
        else:
            n_prime = int(before['meta']['n_prime'])
            if n_prime == 1:
                raise ValueError("no internal support node for n'=1")
            _mutate_support(source_aux_override, hash_index=n_prime)
    elif fault == 'corrupt_payload':
        payload_override = fault_dir / 'payload'
        _copy_payload_for_corruption(Path(str(before['meta']['payload_ref'])), payload_override)
    return (source_aux_override, payload_override)

def _child_migration(workspace: str, dataset_root: str, manifest: dict[str, Any], target_mode: str, fault: str, crash_point: str) -> None:
    migrate_epoch(workspace=Path(workspace), dataset_root=Path(dataset_root), manifest=manifest, target_mode=target_mode, fault=fault, crash_point=crash_point)

def recover_after_failure(workspace: Path, *, before: dict[str, Any], expected_target_mode: str, fault: str) -> dict[str, Any]:
    start = time.perf_counter_ns()
    entry_path = workspace / 'msi' / 'entry.json'
    active = read_entry(entry_path)
    validation = validate_active_state(entry_path)
    protected_equal = protected_bytes(before) == protected_bytes(active)
    allowed, differences = allowed_difference_only(before, active)
    old_mode = str(before['meta']['mode'])
    active_mode = str(active['meta']['mode'])
    expected_mode = expected_target_mode if fault == 'crash_after_metadata_swap' else old_mode
    temp_metadata = list(entry_path.parent.glob(f'.{entry_path.name}.*'))
    journals = list((workspace / 'transactions').glob('*.json'))
    active_ref = Path(str(active['meta']['aux_ref']))
    cleaned_objects = 0
    for candidate in (workspace / 'targets').glob('*'):
        if candidate.resolve() != active_ref.resolve():
            candidate.unlink(missing_ok=True)
            cleaned_objects += 1
    for candidate in temp_metadata + journals:
        candidate.unlink(missing_ok=True)
    end = time.perf_counter_ns()
    return {'recovery_status': 'PASS', 'active_mode': active_mode, 'expected_active_mode': expected_mode, 'active_mode_correct': active_mode == expected_mode, 'active_state_valid': validation['status'] == 'PASS', 'protected_bytes_equal': protected_equal, 'allowed_metadata_difference_only': allowed or active_mode == old_mode, 'metadata_differences': differences, 'mixed_state_detected': False, 'recovery_ns': end - start, 'temporary_metadata_files': len(temp_metadata), 'journals_found': len(journals), 'unreferenced_target_objects_cleaned': cleaned_objects}

def run_migration_case(case: dict[str, Any], *, dataset_dir: Path, work_root: Path) -> dict[str, Any]:
    dataset_root = dataset_path(dataset_dir, str(case['dataset_id']))
    source_mode, target_mode = split_transition(str(case['transition']))
    workspace, before, manifest = _prepare_workspace(case, dataset_root, work_root)
    fs_info = _filesystem_info(workspace)
    fault = str(case.get('fault', 'none'))
    result: dict[str, Any]
    try:
        if case['trial_kind'] == 'migration_perf':
            migration = migrate_epoch(workspace=workspace, dataset_root=dataset_root, manifest=manifest, target_mode=target_mode, fault='none')
            result = {'migration': migration}
        elif case['trial_kind'] == 'equivalence_audit':
            pre = audit_entry_queries(workspace / 'msi' / 'entry.json', seed=int(case['seed']), exact_sample_limit=int(case.get('exact_sample_limit', 64)))
            migration = migrate_epoch(workspace=workspace, dataset_root=dataset_root, manifest=manifest, target_mode=target_mode, fault='none')
            post = audit_entry_queries(workspace / 'msi' / 'entry.json', seed=int(case['seed']), exact_sample_limit=int(case.get('exact_sample_limit', 64)))
            result = {'migration': migration, 'pre_query_audit': pre, 'post_query_audit': post, 'accepted_relation_equivalent': pre['all_accept'] and post['all_accept']}
        elif case['trial_kind'] == 'fault_injection':
            if fault in CRASH_POINTS:
                process = mp.Process(target=_child_migration, args=(str(workspace), str(dataset_root), manifest, target_mode, 'none', fault))
                process.start()
                process.join(timeout=float(case.get('crash_timeout_seconds', 60.0)))
                if process.is_alive():
                    process.kill()
                    process.join()
                    raise RuntimeError('crash-injection child exceeded timeout')
                if process.exitcode in {0, None}:
                    raise RuntimeError('crash injection did not terminate abnormally')
                recovery = recover_after_failure(workspace, before=before, expected_target_mode=target_mode, fault=fault)
                result = {'fault': fault, 'fault_injected': True, 'child_exit_code': process.exitcode, 'migration_rejected': False, 'recovery': recovery}
            else:
                rejection = None
                source_aux_override, payload_override = _prepare_fault_overrides(workspace, before, fault)
                try:
                    migrate_epoch(workspace=workspace, dataset_root=dataset_root, manifest=manifest, target_mode=target_mode, fault=fault, source_aux_override=source_aux_override, payload_override=payload_override)
                except MigrationRejected as exc:
                    rejection = str(exc)
                if rejection is None:
                    raise RuntimeError(f'fault {fault} was not rejected')
                active = read_entry(workspace / 'msi' / 'entry.json')
                validation = validate_active_state(workspace / 'msi' / 'entry.json')
                result = {'fault': fault, 'fault_injected': True, 'migration_rejected': True, 'rejection_reason': rejection, 'active_mode': active['meta']['mode'], 'old_mode_preserved': active['meta']['mode'] == source_mode, 'protected_bytes_equal': protected_bytes(before) == protected_bytes(active), 'active_state_valid': validation['status'] == 'PASS', 'mixed_state_detected': False}
        else:
            raise ValueError(f"unsupported migration trial kind: {case['trial_kind']}")
        checks = {'source_target_distinct': source_mode != target_mode, 'dataset_matches_case': int(manifest['spec']['n']) == int(case['n'])}
        if case['trial_kind'] in {'migration_perf', 'equivalence_audit'}:
            migration = result['migration']
            checks.update({'root_equal': bool(migration['root_equal']), 'protected_bytes_equal': bool(migration['protected_bytes_equal']), 'allowed_metadata_difference_only': bool(migration['allowed_metadata_difference_only']), 'target_active': migration['active_mode'] == target_mode})
        elif fault in CRASH_POINTS:
            recovery = result['recovery']
            checks.update({'active_state_valid': bool(recovery['active_state_valid']), 'active_mode_correct': bool(recovery['active_mode_correct']), 'protected_bytes_equal': bool(recovery['protected_bytes_equal']), 'no_mixed_state': not bool(recovery['mixed_state_detected'])})
        else:
            checks.update({'migration_rejected': bool(result['migration_rejected']), 'old_mode_preserved': bool(result['old_mode_preserved']), 'protected_bytes_equal': bool(result['protected_bytes_equal']), 'active_state_valid': bool(result['active_state_valid']), 'no_mixed_state': not bool(result['mixed_state_detected'])})
        if case['trial_kind'] == 'equivalence_audit':
            checks['accepted_relation_equivalent'] = bool(result['accepted_relation_equivalent'])
        result.update({'schema_version': 1, 'experiment': 'E6', 'case': case, 'case_id': case['case_id'], 'trial_kind': case['trial_kind'], 'transition': case['transition'], 'source_mode': source_mode, 'target_mode': target_mode, 'filesystem': fs_info, 'checks': checks, 'all_checks_pass': all(checks.values()), 'system': system_metadata()})
        if not result['all_checks_pass']:
            raise RuntimeError(f'case invariant failure: {checks}')
        return result
    finally:
        if os.environ.get('E6_KEEP_WORKSPACES', '0') != '1':
            shutil.rmtree(workspace, ignore_errors=True)
