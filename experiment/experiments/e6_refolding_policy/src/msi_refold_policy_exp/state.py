from __future__ import annotations
import base64
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any
from .crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from .merkle import HashCounter, IOStats, FormatError, read_support
from .util import atomic_write_json, read_json
ROOT_STMT = struct.Struct('>8sIQQ32s')
ROOT_STMT_MAGIC = b'E6ROOT1\x00'
REGISTRY_TAG = b'MSI-E6-EXT-REGISTRY\x00'

class StateError(ValueError):
    pass

def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')

def root_statement_bytes(*, shard: int, epoch: int, k: int, root: bytes) -> bytes:
    if len(root) != 32:
        raise ValueError('root must be 32 bytes')
    return ROOT_STMT.pack(ROOT_STMT_MAGIC, int(shard), int(epoch), int(k), root)

def make_anchor(*, shard: int, epoch: int, k: int, root: bytes, key_seed: int) -> dict[str, Any]:
    statement = root_statement_bytes(shard=shard, epoch=epoch, k=k, root=root)
    with Ed25519Signer(deterministic_seed('e6-anchor', key_seed)) as signer:
        signature = signer.sign(statement)
        public_key = signer.public_key
    return {'scheme': 'ed25519-direct-v1', 'statement_b64': base64.b64encode(statement).decode('ascii'), 'public_key_b64': base64.b64encode(public_key).decode('ascii'), 'signature_b64': base64.b64encode(signature).decode('ascii')}

def verify_anchor(anchor: dict[str, Any], *, shard: int, epoch: int, k: int, root: bytes) -> bool:
    try:
        if anchor.get('scheme') != 'ed25519-direct-v1':
            return False
        statement = base64.b64decode(anchor['statement_b64'], validate=True)
        public_key = base64.b64decode(anchor['public_key_b64'], validate=True)
        signature = base64.b64decode(anchor['signature_b64'], validate=True)
        if statement != root_statement_bytes(shard=shard, epoch=epoch, k=k, root=root):
            return False
        with Ed25519Verifier(public_key) as verifier:
            return verifier.verify(statement, signature)
    except Exception:
        return False

def make_entry(*, root: bytes, shard: int, epoch: int, n: int, n_prime: int, k: int, payload_ref: str, layout: str, codec: str, version: int, mode: str, aux_ref: str, anchor: dict[str, Any]) -> dict[str, Any]:
    if mode not in {'full', 'leaf', 'ext'}:
        raise ValueError(mode)
    return {'schema_version': 1, 'root_hex': root.hex(), 'meta': {'shard': int(shard), 'epoch': int(epoch), 'n': int(n), 'n_prime': int(n_prime), 'k': int(k), 'payload_ref': str(payload_ref), 'layout': str(layout), 'codec': str(codec), 'version': int(version), 'mode': mode, 'aux_ref': str(aux_ref)}, 'anchor': anchor}

def validate_entry_shape(entry: dict[str, Any]) -> None:
    if entry.get('schema_version') != 1:
        raise StateError('unsupported MSI entry schema')
    try:
        root = bytes.fromhex(str(entry['root_hex']))
        meta = entry['meta']
        mode = meta['mode']
    except Exception as exc:
        raise StateError('malformed MSI entry') from exc
    if len(root) != 32:
        raise StateError('invalid root length')
    if mode not in {'full', 'leaf', 'ext'}:
        raise StateError('invalid mode')
    required = {'shard', 'epoch', 'n', 'n_prime', 'k', 'payload_ref', 'layout', 'codec', 'version', 'mode', 'aux_ref'}
    if set(meta) != required:
        raise StateError(f'unexpected metadata fields: {sorted(set(meta) ^ required)}')
    if not verify_anchor(entry['anchor'], shard=int(meta['shard']), epoch=int(meta['epoch']), k=int(meta['k']), root=root):
        raise StateError('anchor verification failed')

def read_entry(path: str | Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise StateError('MSI entry is not an object')
    validate_entry_shape(value)
    return value

def write_entry(path: str | Path, entry: dict[str, Any]) -> None:
    validate_entry_shape(entry)
    atomic_write_json(path, entry)

def protected_projection(entry: dict[str, Any]) -> dict[str, Any]:
    validate_entry_shape(entry)
    meta = dict(entry['meta'])
    meta.pop('mode')
    meta.pop('aux_ref')
    return {'schema_version': entry['schema_version'], 'root_hex': entry['root_hex'], 'meta': meta, 'anchor': entry['anchor']}

def protected_bytes(entry: dict[str, Any]) -> bytes:
    return canonical_json_bytes(protected_projection(entry))

def allowed_difference_only(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, list[str]]:
    before_flat = _flatten(before)
    after_flat = _flatten(after)
    differences = sorted((key for key in set(before_flat) | set(after_flat) if before_flat.get(key) != after_flat.get(key)))
    allowed = {'meta.mode', 'meta.aux_ref'}
    return (set(differences).issubset(allowed) and 'meta.mode' in differences, differences)

def _flatten(value: Any, prefix: str='') -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    output: dict[str, Any] = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            output.update(_flatten(item, name))
        else:
            output[name] = item
    return output

def registry_token(*, root: bytes, locator: str, service_support_ref: str) -> str:
    return hashlib.sha256(REGISTRY_TAG + root + len(locator.encode()).to_bytes(4, 'big') + locator.encode() + len(service_support_ref.encode()).to_bytes(4, 'big') + service_support_ref.encode()).hexdigest()

def create_ext_registry(path: str | Path, *, root: bytes, locator: str, service_support_ref: str, incompatible: bool=False, io_stats: IOStats | None=None) -> dict[str, Any]:
    advertised_root = bytes([root[0] ^ 1]) + root[1:] if incompatible else root
    registry = {'schema_version': 1, 'locator': locator, 'root_hex': advertised_root.hex(), 'service_support_ref': str(service_support_ref)}
    registry['compatibility_token'] = registry_token(root=advertised_root, locator=locator, service_support_ref=str(service_support_ref))
    encoded = canonical_json_bytes(registry) + b'\n'
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if io_stats is not None:
        io_stats.write(len(encoded))
    return registry

def validate_ext_registry(path: str | Path, *, expected_root: bytes, io_stats: IOStats | None=None, counter: HashCounter | None=None, verify_service: bool=True) -> dict[str, Any]:
    target = Path(path)
    raw = target.read_bytes()
    if io_stats is not None:
        io_stats.read(len(raw))
    try:
        registry = json.loads(raw)
    except Exception as exc:
        raise StateError('invalid external registry JSON') from exc
    if registry.get('schema_version') != 1:
        raise StateError('unsupported external registry version')
    try:
        root = bytes.fromhex(str(registry['root_hex']))
        locator = str(registry['locator'])
        service_ref = str(registry['service_support_ref'])
        token = str(registry['compatibility_token'])
    except Exception as exc:
        raise StateError('malformed external registry') from exc
    if root != expected_root:
        raise StateError('external registry root mismatch')
    if token != registry_token(root=root, locator=locator, service_support_ref=service_ref):
        raise StateError('external registry token mismatch')
    if verify_service:
        support = read_support(service_ref, expected_mode='full', counter=counter, io_stats=io_stats, verify=True)
        if support.root != expected_root:
            raise StateError('external witness service is incompatible')
    return registry

def validate_active_state(entry_path: str | Path, *, verify_service: bool=True) -> dict[str, Any]:
    entry = read_entry(entry_path)
    meta = entry['meta']
    root = bytes.fromhex(entry['root_hex'])
    mode = str(meta['mode'])
    counter = HashCounter()
    io_stats = IOStats()
    if mode in {'full', 'leaf'}:
        support = read_support(meta['aux_ref'], expected_mode=mode, counter=counter, io_stats=io_stats, verify=True)
        if support.root != root or support.n != int(meta['n']):
            raise StateError('active support object does not match MSI')
    else:
        validate_ext_registry(meta['aux_ref'], expected_root=root, io_stats=io_stats, counter=counter, verify_service=verify_service)
    return {'status': 'PASS', 'mode': mode, 'root_hex': root.hex(), 'hashes': counter.as_dict(), 'io': io_stats.as_dict(), 'protected_sha256': hashlib.sha256(protected_bytes(entry)).hexdigest()}
