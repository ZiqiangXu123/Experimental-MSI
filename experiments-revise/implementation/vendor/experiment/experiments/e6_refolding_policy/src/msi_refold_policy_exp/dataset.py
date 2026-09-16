from __future__ import annotations
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from .merkle import HashCounter, PayloadStore, build_layers, build_payload_store, leaves_from_payload, next_power_of_two, parse_support_bytes, serialize_full_support, serialize_leaf_support, support_size_formula, verify_path, path_from_layers
from .state import create_ext_registry, make_anchor, make_entry, validate_ext_registry, write_entry
from .util import atomic_write_json, read_json, sha256_file, stable_seed

@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    shard: int
    epoch: int
    n: int
    block_bytes: int
    layout: str
    codec: str
    payload_seed: int
    anchor_key_seed: int

    @property
    def n_prime(self) -> int:
        return next_power_of_two(self.n)

    def as_dict(self) -> dict[str, Any]:
        return {'dataset_id': self.dataset_id, 'shard': self.shard, 'epoch': self.epoch, 'n': self.n, 'n_prime': self.n_prime, 'block_bytes': self.block_bytes, 'layout': self.layout, 'codec': self.codec, 'payload_seed': self.payload_seed, 'anchor_key_seed': self.anchor_key_seed}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'DatasetSpec':
        return cls(dataset_id=str(value['dataset_id']), shard=int(value['shard']), epoch=int(value['epoch']), n=int(value['n']), block_bytes=int(value['block_bytes']), layout=str(value['layout']), codec=str(value['codec']), payload_seed=int(value['payload_seed']), anchor_key_seed=int(value['anchor_key_seed']))

def make_dataset_spec(*, shard: int, epoch: int, n: int, block_bytes: int, layout: str, codec: str, payload_seed: int, anchor_key_seed: int) -> DatasetSpec:
    identity = {'shard': shard, 'epoch': epoch, 'n': n, 'block_bytes': block_bytes, 'layout': layout, 'codec': codec, 'payload_seed': payload_seed, 'anchor_key_seed': anchor_key_seed}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    return DatasetSpec(dataset_id=f'ds-{digest}', **identity)

def dataset_path(dataset_dir: str | Path, spec_or_id: DatasetSpec | str) -> Path:
    identifier = spec_or_id.dataset_id if isinstance(spec_or_id, DatasetSpec) else str(spec_or_id)
    return Path(dataset_dir) / identifier

def _relative_manifest_files(root: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for path in sorted((p for p in root.rglob('*') if p.is_file() and p.name != 'manifest.json')):
        output[path.relative_to(root).as_posix()] = sha256_file(path)
    return output

def build_dataset(dataset_dir: str | Path, spec: DatasetSpec, *, force: bool=False) -> dict[str, Any]:
    final = dataset_path(dataset_dir, spec)
    if final.exists() and (not force):
        return validate_dataset(final, spec, verify_hashes=False)
    staging = final.with_name(f'.{final.name}.build-{os.getpid()}')
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        payload_root = staging / 'payload'
        payload_manifest = build_payload_store(payload_root, shard=spec.shard, epoch=spec.epoch, n=spec.n, block_bytes=spec.block_bytes, layout=spec.layout, codec=spec.codec, seed=spec.payload_seed)
        counter = HashCounter()
        leaves, _ = leaves_from_payload(payload_root, counter=counter)
        layers = build_layers(leaves)
        root = layers[-1][0]
        aux_dir = staging / 'aux'
        aux_dir.mkdir()
        full_path = aux_dir / 'full.bin'
        leaf_path = aux_dir / 'leaf.bin'
        full_data = serialize_full_support(n=spec.n, layers=layers)
        leaf_data = serialize_leaf_support(n=spec.n, leaves=leaves, root=root)
        full_path.write_bytes(full_data)
        leaf_path.write_bytes(leaf_data)
        with full_path.open('rb') as handle:
            os.fsync(handle.fileno())
        with leaf_path.open('rb') as handle:
            os.fsync(handle.fileno())
        external_dir = staging / 'external'
        external_dir.mkdir()
        external_support = external_dir / 'witness_full.bin'
        shutil.copyfile(full_path, external_support)
        registry_path = external_dir / 'registry.json'
        create_ext_registry(registry_path, root=root, locator=f'e6://witness/{spec.dataset_id}', service_support_ref=str((final / 'external' / 'witness_full.bin').resolve()))
        anchor = make_anchor(shard=spec.shard, epoch=spec.epoch, k=spec.epoch, root=root, key_seed=spec.anchor_key_seed)
        templates = staging / 'templates'
        templates.mkdir()
        common = dict(root=root, shard=spec.shard, epoch=spec.epoch, n=spec.n, n_prime=spec.n_prime, k=spec.epoch, payload_ref=str((final / 'payload').resolve()), layout=spec.layout, codec=spec.codec, version=1, anchor=anchor)
        write_entry(templates / 'full.json', make_entry(mode='full', aux_ref=str((final / 'aux' / 'full.bin').resolve()), **common))
        write_entry(templates / 'leaf.json', make_entry(mode='leaf', aux_ref=str((final / 'aux' / 'leaf.bin').resolve()), **common))
        write_entry(templates / 'ext.json', make_entry(mode='ext', aux_ref=str((final / 'external' / 'registry.json').resolve()), **common))
        manifest = {'schema_version': 1, 'spec': spec.as_dict(), 'root_hex': root.hex(), 'payload': payload_manifest, 'hash_build_counts': counter.as_dict(), 'objects': {'payload_root': 'payload', 'full_support': 'aux/full.bin', 'leaf_support': 'aux/leaf.bin', 'external_support': 'external/witness_full.bin', 'external_registry': 'external/registry.json', 'entry_full': 'templates/full.json', 'entry_leaf': 'templates/leaf.json', 'entry_ext': 'templates/ext.json'}, 'sizes': {'full_serialized_bytes': len(full_data), 'leaf_serialized_bytes': len(leaf_data), 'full_formula_bytes': support_size_formula('full', spec.n), 'leaf_formula_bytes': support_size_formula('leaf', spec.n)}}
        manifest['file_sha256'] = _relative_manifest_files(staging)
        atomic_write_json(staging / 'manifest.json', manifest)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
        return validate_dataset(final, spec, verify_hashes=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

def load_manifest(root: str | Path) -> dict[str, Any]:
    value = read_json(Path(root) / 'manifest.json')
    if not isinstance(value, dict):
        raise ValueError('dataset manifest is not an object')
    return value

def resolve_object(root: str | Path, manifest: dict[str, Any], name: str) -> Path:
    return Path(root) / str(manifest['objects'][name])

def validate_dataset(root: str | Path, spec: DatasetSpec | None=None, *, verify_hashes: bool=False) -> dict[str, Any]:
    target = Path(root)
    manifest = load_manifest(target)
    got_spec = DatasetSpec.from_dict(manifest['spec'])
    if spec is not None and got_spec != spec:
        raise ValueError('dataset spec mismatch')
    root_digest = bytes.fromhex(manifest['root_hex'])
    full_data = resolve_object(target, manifest, 'full_support').read_bytes()
    leaf_data = resolve_object(target, manifest, 'leaf_support').read_bytes()
    full = parse_support_bytes(full_data, expected_mode='full', verify=True)
    leaf = parse_support_bytes(leaf_data, expected_mode='leaf', verify=True)
    if full.root != root_digest or leaf.root != root_digest:
        raise ValueError('dataset support roots disagree')
    if len(full_data) != support_size_formula('full', got_spec.n):
        raise ValueError('full support size formula mismatch')
    if len(leaf_data) != support_size_formula('leaf', got_spec.n):
        raise ValueError('leaf support size formula mismatch')
    validate_ext_registry(resolve_object(target, manifest, 'external_registry'), expected_root=root_digest, verify_service=True)
    store = PayloadStore(resolve_object(target, manifest, 'payload_root'))
    positions = sorted({1, got_spec.n, (got_spec.n + 1) // 2})
    layers = list(full.layers) if full.layers else [[root_digest]]
    for position in positions:
        block = store.read_block(position)
        if got_spec.n_prime == 1:
            path = []
        else:
            path = path_from_layers(layers, position)
        if not verify_path(shard=got_spec.shard, epoch=got_spec.epoch, position=position, block=block, path=path, expected_root=root_digest):
            raise ValueError('dataset sample query failed')
    hash_failures: list[str] = []
    if verify_hashes:
        for relative, expected in manifest.get('file_sha256', {}).items():
            path = target / relative
            if not path.is_file() or sha256_file(path) != expected:
                hash_failures.append(relative)
    return {'status': 'PASS' if not hash_failures else 'FAIL', 'dataset_id': got_spec.dataset_id, 'root_hex': root_digest.hex(), 'n': got_spec.n, 'n_prime': got_spec.n_prime, 'layout': got_spec.layout, 'codec': got_spec.codec, 'files_checked': len(manifest.get('file_sha256', {})) if verify_hashes else 0, 'hash_failures': hash_failures, 'full_serialized_bytes': len(full_data), 'leaf_serialized_bytes': len(leaf_data)}

def specs_from_plan(path: str | Path) -> list[DatasetSpec]:
    from .util import jsonl_iter
    return [DatasetSpec.from_dict(row) for row in jsonl_iter(path)]
