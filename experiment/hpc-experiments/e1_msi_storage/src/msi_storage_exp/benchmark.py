from __future__ import annotations
import math
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from .constants import ANCHOR_FILE_HEADER_BYTES, ANCHOR_RECORD_BYTES, AUX_VALUE_HEADER_BYTES, CANONICAL_BLOCK_HEADER_BYTES, DIGEST_BYTES, MSI_ENTRY_BYTES, MSI_FILE_HEADER_BYTES, OBJECT_RECORD_OVERHEAD_BYTES, OBJECT_STORE_HEADER_BYTES, PAYLOAD_VALUE_HEADER_BYTES, ROOT_INDEX_FILE_HEADER_BYTES, ROOT_INDEX_RECORD_BYTES
from .encoding import build_merkle, canonical_block, encode_anchor_record, encode_aux_value, encode_msi_entry, encode_payload_values, encode_root_index_record, leaf_hash, object_ref, synthetic_root
from .store import FixedRecordFile, ObjectStoreFile
from .util import atomic_write_json, command_output, environment_inventory, next_power_of_two, scratch_directory

def raw_payload_model_per_epoch(n: int, block_bytes: int, layout: str) -> tuple[int, int, int]:
    if layout == 'per_block':
        objects = n
        value_bytes = n * (PAYLOAD_VALUE_HEADER_BYTES + block_bytes)
    elif layout == 'epoch_packed':
        objects = 1
        canonical_epoch = 4 + (n + 1) * 8 + n * block_bytes
        value_bytes = PAYLOAD_VALUE_HEADER_BYTES + canonical_epoch
    else:
        raise KeyError(layout)
    store_bytes = objects * OBJECT_RECORD_OVERHEAD_BYTES + value_bytes
    return (store_bytes, value_bytes, objects)

def aux_model_per_epoch(n: int, mode: str) -> tuple[int, int, int, int]:
    n_prime = next_power_of_two(n)
    if mode == 'full':
        count = 2 * n_prime - 2
    elif mode == 'leaf':
        count = n_prime
    elif mode == 'ext':
        return (0, 0, 0, 0)
    else:
        raise KeyError(mode)
    hash_bytes = count * DIGEST_BYTES
    value_bytes = AUX_VALUE_HEADER_BYTES + hash_bytes
    store_bytes = OBJECT_RECORD_OVERHEAD_BYTES + value_bytes
    return (store_bytes, value_bytes, 1, hash_bytes)

def estimate_materialized_bytes(point: dict[str, Any]) -> int:
    shards = point['shards']
    t = point['historical_epochs']
    n = point['epoch_length']
    hot = point['hot_window']
    block = point['block_bytes']
    layout = point['layout']
    total = 0
    components = set(point['materialize'])
    if 'msi' in components:
        total += 3 * (shards * MSI_FILE_HEADER_BYTES + shards * t * MSI_ENTRY_BYTES)
    if 'root_index' in components:
        total += shards * ROOT_INDEX_FILE_HEADER_BYTES + shards * t * ROOT_INDEX_RECORD_BYTES
    if 'anchor' in components:
        total += ANCHOR_FILE_HEADER_BYTES + shards * ANCHOR_RECORD_BYTES
    payload_store, _, _ = raw_payload_model_per_epoch(n, block, layout)
    payload_guard = payload_store if point['codec'] == 'raw-v1' else int(payload_store * 1.1) + 64
    if 'payload' in components:
        total += OBJECT_STORE_HEADER_BYTES + shards * t * payload_guard
    if 'aux' in components:
        full_store, _, _, _ = aux_model_per_epoch(n, 'full')
        leaf_store, _, _, _ = aux_model_per_epoch(n, 'leaf')
        total += 2 * OBJECT_STORE_HEADER_BYTES + shards * t * (full_store + leaf_store)
    if 'hot' in components:
        full_store, _, _, _ = aux_model_per_epoch(n, 'full')
        total += 2 * OBJECT_STORE_HEADER_BYTES + shards * hot * (payload_guard + full_store)
    return total

def _component_template(logical: int, records: int, materialized: bool=False, allocated: int | None=None, value_bytes: int | None=None) -> dict[str, Any]:
    return {'logical_bytes': int(logical), 'allocated_bytes': int(allocated) if allocated is not None else None, 'records': int(records), 'materialized': bool(materialized), 'value_bytes': int(value_bytes) if value_bytes is not None else None}

def _model_components(point: dict[str, Any]) -> dict[str, dict[str, Any]]:
    s = point['shards']
    t = point['historical_epochs']
    n = point['epoch_length']
    hot = point['hot_window']
    block = point['block_bytes']
    layout = point['layout']
    codec = point['codec']
    epochs_total = s * t
    hot_total = s * hot
    components: dict[str, dict[str, Any]] = {}
    msi_logical = s * MSI_FILE_HEADER_BYTES + epochs_total * MSI_ENTRY_BYTES
    for mode in ('full', 'leaf', 'ext'):
        components[f'msi_{mode}'] = _component_template(msi_logical, epochs_total)
    root_index_logical = s * ROOT_INDEX_FILE_HEADER_BYTES + epochs_total * ROOT_INDEX_RECORD_BYTES
    components['root_index'] = _component_template(root_index_logical, epochs_total)
    anchor_logical = ANCHOR_FILE_HEADER_BYTES + s * ANCHOR_RECORD_BYTES
    components['anchor'] = _component_template(anchor_logical, s)
    if codec == 'raw-v1':
        p_store, p_value, p_objects = raw_payload_model_per_epoch(n, block, layout)
        payload_logical = OBJECT_STORE_HEADER_BYTES + epochs_total * p_store
        components['historical_payload'] = _component_template(payload_logical, epochs_total * p_objects, value_bytes=epochs_total * p_value)
        hot_payload_logical = OBJECT_STORE_HEADER_BYTES + hot_total * p_store if hot_total else 0
        components['hot_payload'] = _component_template(hot_payload_logical, hot_total * p_objects, value_bytes=hot_total * p_value)
    else:
        components['historical_payload'] = _component_template(0, 0)
        components['hot_payload'] = _component_template(0, 0)
    for mode in ('full', 'leaf'):
        a_store, a_value, a_objects, hash_bytes = aux_model_per_epoch(n, mode)
        logical = OBJECT_STORE_HEADER_BYTES + epochs_total * a_store
        components[f'historical_aux_{mode}'] = _component_template(logical, epochs_total * a_objects, value_bytes=epochs_total * a_value)
        components[f'historical_aux_{mode}']['hash_bytes'] = epochs_total * hash_bytes
    components['historical_aux_ext'] = _component_template(0, 0, value_bytes=0)
    components['historical_aux_ext']['hash_bytes'] = 0
    full_store, full_value, full_objects, full_hash = aux_model_per_epoch(n, 'full')
    hot_aux_logical = OBJECT_STORE_HEADER_BYTES + hot_total * full_store if hot_total else 0
    components['hot_aux_full'] = _component_template(hot_aux_logical, hot_total * full_objects, value_bytes=hot_total * full_value)
    components['hot_aux_full']['hash_bytes'] = hot_total * full_hash
    return components

def _measure_file(component: dict[str, Any], measurement, value_bytes: int | None=None) -> None:
    component.update({'logical_bytes': measurement.logical_bytes, 'allocated_bytes': measurement.allocated_bytes, 'records': measurement.records, 'materialized': True})
    if value_bytes is not None:
        component['value_bytes'] = value_bytes

def _materialize_fixed_components(point: dict[str, Any], work: Path, components: dict[str, dict[str, Any]], actual_roots: dict[tuple[int, int], bytes] | None=None) -> None:
    s_count = point['shards']
    t_count = point['historical_epochs']
    n = point['epoch_length']
    block_bytes = point['block_bytes']
    layout = point['layout']
    codec = point['codec']
    seed = point['seed']
    wanted = set(point['materialize'])
    msi_files = {}
    if 'msi' in wanted:
        for mode in ('full', 'leaf', 'ext'):
            msi_files[mode] = []
            for shard in range(1, s_count + 1):
                msi_files[mode].append(FixedRecordFile(work / f'msi_{mode}_shard_{shard:04d}.bin', b'MSI-E1'))
    root_files = []
    if 'root_index' in wanted:
        for shard in range(1, s_count + 1):
            root_files.append(FixedRecordFile(work / f'root_shard_{shard:04d}.bin', b'ROOT-E1'))
    anchor_file = FixedRecordFile(work / 'anchors.bin', b'ANCH-E1') if 'anchor' in wanted else None
    for shard in range(1, s_count + 1):
        last_root = synthetic_root(seed, shard, max(1, t_count), n, block_bytes)
        for epoch in range(1, t_count + 1):
            root = (actual_roots or {}).get((shard, epoch), synthetic_root(seed, shard, epoch, n, block_bytes))
            payload_ref = object_ref('payload', shard, epoch, f'{layout}:{codec}')
            for mode in ('full', 'leaf', 'ext'):
                if 'msi' in wanted:
                    aux_ref = object_ref('aux', shard, epoch, mode)
                    record = encode_msi_entry(epoch_position=epoch, n=n, root=root, payload_ref=payload_ref, aux_ref=aux_ref, layout=layout, mode=mode, codec=codec)
                    msi_files[mode][shard - 1].append(record)
            if 'root_index' in wanted:
                root_files[shard - 1].append(encode_root_index_record(epoch, root))
            last_root = root
        if anchor_file is not None:
            anchor_file.append(encode_anchor_record(shard, t_count, last_root, seed))
    if 'msi' in wanted:
        for mode in ('full', 'leaf', 'ext'):
            measurements = [f.close() for f in msi_files[mode]]
            logical = sum((m.logical_bytes for m in measurements))
            allocated = sum((m.allocated_bytes for m in measurements))
            records = sum((m.records for m in measurements))
            components[f'msi_{mode}'].update(logical_bytes=logical, allocated_bytes=allocated, records=records, materialized=True)
    if 'root_index' in wanted:
        measurements = [f.close() for f in root_files]
        components['root_index'].update(logical_bytes=sum((m.logical_bytes for m in measurements)), allocated_bytes=sum((m.allocated_bytes for m in measurements)), records=sum((m.records for m in measurements)), materialized=True)
    if anchor_file is not None:
        measurement = anchor_file.close()
        _measure_file(components['anchor'], measurement)

def _epoch_material(*, shard: int, epoch: int, n: int, block_bytes: int, seed: int) -> tuple[list[bytes], Any]:
    blocks = [canonical_block(shard=shard, epoch=epoch, position=i, block_bytes=block_bytes, seed=seed) for i in range(1, n + 1)]
    leaves = [leaf_hash(shard, epoch, i, block) for i, block in enumerate(blocks, start=1)]
    return (blocks, build_merkle(leaves))

def _materialize_historical_objects(point: dict[str, Any], work: Path, components: dict[str, dict[str, Any]]) -> dict[tuple[int, int], bytes]:
    wanted = set(point['materialize'])
    if not {'payload', 'aux'} & wanted:
        return {}
    s_count = point['shards']
    t_count = point['historical_epochs']
    n = point['epoch_length']
    block_bytes = point['block_bytes']
    layout = point['layout']
    codec = point['codec']
    seed = point['seed']
    payload_store = ObjectStoreFile(work / 'historical_payload.bin', b'PAY-E1') if 'payload' in wanted else None
    full_store = ObjectStoreFile(work / 'historical_aux_full.bin', b'AUXF-E1') if 'aux' in wanted else None
    leaf_store = ObjectStoreFile(work / 'historical_aux_leaf.bin', b'AUXL-E1') if 'aux' in wanted else None
    full_hash_bytes = 0
    leaf_hash_bytes = 0
    actual_roots: dict[tuple[int, int], bytes] = {}
    for shard in range(1, s_count + 1):
        for epoch in range(1, t_count + 1):
            blocks, merkle = _epoch_material(shard=shard, epoch=epoch, n=n, block_bytes=block_bytes, seed=seed)
            actual_roots[shard, epoch] = merkle.root
            if payload_store is not None:
                for object_number, value in enumerate(encode_payload_values(blocks=blocks, layout=layout, codec=codec), start=1):
                    key = object_ref('payload', shard, epoch, f'{layout}:{codec}:{object_number}')
                    payload_store.append(key, value)
            if full_store is not None and leaf_store is not None:
                full_value = encode_aux_value('full', n, merkle.n_prime, merkle.non_root_nodes)
                leaf_value = encode_aux_value('leaf', n, merkle.n_prime, merkle.padded_leaves)
                full_store.append(object_ref('aux', shard, epoch, 'full'), full_value)
                leaf_store.append(object_ref('aux', shard, epoch, 'leaf'), leaf_value)
                full_hash_bytes += len(merkle.non_root_nodes) * DIGEST_BYTES
                leaf_hash_bytes += len(merkle.padded_leaves) * DIGEST_BYTES
    if payload_store is not None:
        measurement, values = payload_store.close()
        _measure_file(components['historical_payload'], measurement, values)
    if full_store is not None and leaf_store is not None:
        measurement, values = full_store.close()
        _measure_file(components['historical_aux_full'], measurement, values)
        components['historical_aux_full']['hash_bytes'] = full_hash_bytes
        measurement, values = leaf_store.close()
        _measure_file(components['historical_aux_leaf'], measurement, values)
        components['historical_aux_leaf']['hash_bytes'] = leaf_hash_bytes
    return actual_roots

def _materialize_hot(point: dict[str, Any], work: Path, components: dict[str, dict[str, Any]]) -> None:
    if 'hot' not in set(point['materialize']) or point['hot_window'] == 0:
        return
    s_count = point['shards']
    t_count = point['historical_epochs']
    hot = point['hot_window']
    n = point['epoch_length']
    block_bytes = point['block_bytes']
    layout = point['layout']
    codec = point['codec']
    seed = point['seed']
    payload_store = ObjectStoreFile(work / 'hot_payload.bin', b'HOTP-E1')
    aux_store = ObjectStoreFile(work / 'hot_aux_full.bin', b'HOTA-E1')
    hash_bytes = 0
    for shard in range(1, s_count + 1):
        for offset in range(1, hot + 1):
            epoch = t_count + offset
            blocks, merkle = _epoch_material(shard=shard, epoch=epoch, n=n, block_bytes=block_bytes, seed=seed)
            for object_number, value in enumerate(encode_payload_values(blocks=blocks, layout=layout, codec=codec), start=1):
                payload_store.append(object_ref('hot-payload', shard, epoch, f'{layout}:{codec}:{object_number}'), value)
            value = encode_aux_value('full', n, merkle.n_prime, merkle.non_root_nodes)
            aux_store.append(object_ref('hot-aux', shard, epoch, 'full'), value)
            hash_bytes += len(merkle.non_root_nodes) * DIGEST_BYTES
    measurement, values = payload_store.close()
    _measure_file(components['hot_payload'], measurement, values)
    measurement, values = aux_store.close()
    _measure_file(components['hot_aux_full'], measurement, values)
    components['hot_aux_full']['hash_bytes'] = hash_bytes

def _scheme_rows(components: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:

    def l(name: str) -> int:
        return int(components[name]['logical_bytes'])

    def alloc(names: list[str]) -> int | None:
        vals: list[int | None] = []
        for name in names:
            comp = components[name]
            if comp['logical_bytes'] == 0:
                vals.append(0)
            else:
                vals.append(comp['allocated_bytes'])
        return sum((int(v) for v in vals)) if all((v is not None for v in vals)) else None
    hot_names = ['hot_payload', 'hot_aux_full']
    hot = sum((l(x) for x in hot_names))
    payload = l('historical_payload')
    full_aux = l('historical_aux_full')
    leaf_aux = l('historical_aux_leaf')
    anchor = l('anchor')
    spec = {'B0_LocalArchive_Raw': {'local': ['historical_payload', 'historical_aux_full', *hot_names], 'external_payload': [], 'external_aux': []}, 'B1_LocalArchive_Verified': {'local': ['historical_payload', 'historical_aux_full', 'msi_full', 'anchor', *hot_names], 'external_payload': [], 'external_aux': []}, 'B2_MSI_Full': {'local': ['msi_full', 'anchor', *hot_names], 'external_payload': ['historical_payload'], 'external_aux': ['historical_aux_full']}, 'B3_MSI_Leaf': {'local': ['msi_leaf', 'anchor', *hot_names], 'external_payload': ['historical_payload'], 'external_aux': ['historical_aux_leaf']}, 'B4_MSI_Ext': {'local': ['msi_ext', 'anchor', *hot_names], 'external_payload': ['historical_payload'], 'external_aux': ['historical_aux_ext']}, 'B5_Root_Anchor_LowerBound': {'local': ['root_index', 'anchor', *hot_names], 'external_payload': ['historical_payload'], 'external_aux': ['historical_aux_ext']}}
    rows: dict[str, dict[str, Any]] = {}
    for name, placement in spec.items():
        local = sum((l(x) for x in placement['local']))
        ext_payload = sum((l(x) for x in placement['external_payload']))
        ext_aux = sum((l(x) for x in placement['external_aux']))
        rows[name] = {'local_logical_bytes': local, 'external_payload_logical_bytes': ext_payload, 'external_aux_logical_bytes': ext_aux, 'total_logical_bytes': local + ext_payload + ext_aux, 'local_allocated_bytes': alloc(placement['local']), 'external_payload_allocated_bytes': alloc(placement['external_payload']), 'external_aux_allocated_bytes': alloc(placement['external_aux'])}
    return rows

def run_point(point: dict[str, Any], *, output_dir: Path, scratch_base: str | None=None, force: bool=False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"{point['point_id']}.json"
    if result_path.exists() and (not force):
        return result_path
    estimate = estimate_materialized_bytes(point)
    if estimate > point['max_materialized_bytes']:
        raise RuntimeError(f"point {point['point_id']} requests approximately {estimate} materialized bytes, above max_materialized_bytes={point['max_materialized_bytes']}")
    start_wall = time.time()
    start_cpu = time.process_time()
    components = _model_components(point)
    with scratch_directory(scratch_base, f"msi-e1-{point['point_id']}-") as tmp:
        work = Path(tmp)
        materialization_filesystem = command_output(['df', '-T', str(work)])
        actual_roots = _materialize_historical_objects(point, work, components)
        _materialize_fixed_components(point, work, components, actual_roots=actual_roots)
        _materialize_hot(point, work, components)
    n = point['epoch_length']
    n_prime = next_power_of_two(n)
    epochs_total = point['shards'] * point['historical_epochs']
    theory_full_hash = epochs_total * (2 * n_prime - 2) * DIGEST_BYTES
    theory_leaf_hash = epochs_total * n_prime * DIGEST_BYTES
    expected_msi = point['shards'] * MSI_FILE_HEADER_BYTES + epochs_total * MSI_ENTRY_BYTES
    expected_root_index = point['shards'] * ROOT_INDEX_FILE_HEADER_BYTES + epochs_total * ROOT_INDEX_RECORD_BYTES
    expected_anchor = ANCHOR_FILE_HEADER_BYTES + point['shards'] * ANCHOR_RECORD_BYTES
    expected_full_store = OBJECT_STORE_HEADER_BYTES + epochs_total * (OBJECT_RECORD_OVERHEAD_BYTES + AUX_VALUE_HEADER_BYTES + (2 * n_prime - 2) * DIGEST_BYTES)
    expected_leaf_store = OBJECT_STORE_HEADER_BYTES + epochs_total * (OBJECT_RECORD_OVERHEAD_BYTES + AUX_VALUE_HEADER_BYTES + n_prime * DIGEST_BYTES)
    if point['codec'] == 'raw-v1':
        payload_per_epoch, _, _ = raw_payload_model_per_epoch(n, point['block_bytes'], point['layout'])
        expected_payload_store = OBJECT_STORE_HEADER_BYTES + epochs_total * payload_per_epoch
        payload_serialization_exact = components['historical_payload']['logical_bytes'] == expected_payload_store
    else:
        expected_payload_store = None
        payload_serialization_exact = bool(components['historical_payload']['materialized'] and components['historical_payload']['logical_bytes'] > OBJECT_STORE_HEADER_BYTES)
    checks = {'msi_modes_equal_logical_size': len({components[f'msi_{m}']['logical_bytes'] for m in ('full', 'leaf', 'ext')}) == 1, 'msi_serialization_exact': all((components[f'msi_{m}']['logical_bytes'] == expected_msi for m in ('full', 'leaf', 'ext'))), 'root_index_serialization_exact': components['root_index']['logical_bytes'] == expected_root_index, 'anchor_serialization_exact': components['anchor']['logical_bytes'] == expected_anchor, 'payload_serialization_exact': payload_serialization_exact, 'full_hash_formula_exact': components['historical_aux_full'].get('hash_bytes') == theory_full_hash, 'leaf_hash_formula_exact': components['historical_aux_leaf'].get('hash_bytes') == theory_leaf_hash, 'ext_hash_formula_exact': components['historical_aux_ext'].get('hash_bytes', 0) == 0, 'full_aux_serialization_exact': components['historical_aux_full']['logical_bytes'] == expected_full_store, 'leaf_aux_serialization_exact': components['historical_aux_leaf']['logical_bytes'] == expected_leaf_store, 'ext_aux_serialization_exact': components['historical_aux_ext']['logical_bytes'] == 0, 'materialized_allocation_recorded': all((not comp['materialized'] or comp['allocated_bytes'] is not None for comp in components.values())), 'padding_power_of_two': n_prime >= n and n_prime & n_prime - 1 == 0}
    result = {'schema_version': 1, 'point': point, 'environment': {**environment_inventory(), 'materialization_filesystem': materialization_filesystem, 'root_generation': 'canonical_merkle' if actual_roots else 'deterministic_size_only_digest'}, 'materialization_estimate_bytes': estimate, 'timing': {'wall_seconds': time.time() - start_wall, 'cpu_seconds': time.process_time() - start_cpu}, 'theory': {'digest_bytes': DIGEST_BYTES, 'n_prime': n_prime, 'depth': int(math.log2(n_prime)), 'msi_entry_bytes': MSI_ENTRY_BYTES, 'anchor_record_bytes': ANCHOR_RECORD_BYTES, 'full_hash_bytes_per_epoch': (2 * n_prime - 2) * DIGEST_BYTES, 'leaf_hash_bytes_per_epoch': n_prime * DIGEST_BYTES, 'ext_hash_bytes_per_epoch': 0, 'full_hash_bytes_total': theory_full_hash, 'leaf_hash_bytes_total': theory_leaf_hash, 'expected_msi_store_bytes': expected_msi, 'expected_root_index_store_bytes': expected_root_index, 'expected_anchor_store_bytes': expected_anchor, 'expected_full_aux_store_bytes': expected_full_store, 'expected_leaf_aux_store_bytes': expected_leaf_store, 'expected_payload_store_bytes': expected_payload_store}, 'components': components, 'schemes': _scheme_rows(components), 'checks': checks}
    atomic_write_json(result_path, result)
    return result_path
