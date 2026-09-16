





from __future__ import annotations

import base64
import binascii
from functools import lru_cache
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from typing import BinaryIO

_SUITE = Path(__file__).resolve().parents[1]
_ORIGINAL_SRC = _SUITE / "vendor/experiment/experiments/e2_local_query/src"
if str(_ORIGINAL_SRC) not in sys.path:
    sys.path.insert(0, str(_ORIGINAL_SRC))

from msi_query_exp.constants import (  
    CANONICAL_HEADER_BYTES, CANONICAL_MAGIC, CANONICAL_VERSION,
)
from msi_query_exp.crypto import Ed25519OpenSSL, Ed25519Verifier  
from msi_query_exp.encoding import (  
    _CANONICAL_HEADER, parse_canonical_block, root_statement,
)
from msi_query_exp.merkle import (  
    build_merkle, extract_path, leaf_hash, rebuild_path, verify_member,
)

MODES = ("archive", "full", "leaf", "ext_cached", "ext_rebuild")
MAX_N = 65536
MAX_PAYLOAD_TOTAL_BYTES = 256 * 1024 * 1024
MAX_CANONICAL_BLOCK_BYTES = MAX_PAYLOAD_TOTAL_BYTES + CANONICAL_HEADER_BYTES
MAX_RESPONSE_BYTES = ((MAX_CANONICAL_BLOCK_BYTES + 2) // 3) * 4 + MAX_N * 70 + 65536
FORMAT_VERSION = 1
PAPER_DESCRIPTOR_BYTES = 126
_BACKEND = Ed25519OpenSSL()


@lru_cache(maxsize=128)
def _trusted_verifier(public: bytes) -> Ed25519Verifier:
    
    return Ed25519Verifier(_BACKEND, public)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, private: bool = False) -> None:
    
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        if private:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def _strict_int(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid integer or integer outside safety bound")
    return value


def _digest(value: object, length: int = 32) -> bytes:
    if not isinstance(value, str) or len(value) != length * 2:
        raise ValueError("invalid fixed-width hexadecimal encoding")
    result = bytes.fromhex(value)
    if len(result) != length:
        raise ValueError("invalid digest length")
    return result


def _context(value: dict) -> tuple[int, int, int, int]:
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    shard = _strict_int(value["shard"], 0, (1 << 32) - 1)
    epoch = _strict_int(value["epoch"], 0, (1 << 64) - 1)
    n = _strict_int(value["n"], 1, MAX_N)
    n_prime = _strict_int(value["n_prime"], 1, MAX_N)
    if n_prime != 1 << (n - 1).bit_length():
        raise ValueError("padding geometry disagrees with original n")
    if _strict_int(value["k"], 0, (1 << 64) - 1) != epoch:
        raise ValueError("this direct-anchor profile uses k=epoch")
    if value["layout"] != "per_block" or value["codec"] != "raw-v1":
        raise ValueError("unsupported persistent payload layout or codec")
    if value["mode"] not in MODES:
        raise ValueError("unsupported storage mode")
    if type(value["version"]) is not int or value["version"] != FORMAT_VERSION:
        raise ValueError("unsupported metadata version")
    _strict_int(value["max_block_bytes"], CANONICAL_HEADER_BYTES,
                MAX_CANONICAL_BLOCK_BYTES)
    return shard, epoch, n, n_prime


def _read_json(path: Path, limit: int = 65536) -> dict:
    def no_duplicate_keys(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("metadata exceeds byte limit")
    value = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    return value


def load_descriptor(path: Path) -> dict:
    
    path = Path(path)
    target = path / "verifier/descriptor.json" if path.is_dir() else path
    descriptor = _read_json(target)
    if descriptor.get("format") != "msi-pi-case":
        raise ValueError("invalid descriptor format")
    _context(descriptor)
    _digest(descriptor["root_hex"])
    _trusted_verifier(_digest(descriptor["public_key_hex"]))
    return descriptor


def create_case(payloads: list[bytes], path: Path, mode: str,
                shard: int = 1, epoch: int = 1) -> dict:
    




    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_N:
        raise ValueError(f"payload count must be between 1 and {MAX_N}")
    if any(type(payload) is not bytes for payload in payloads):
        raise TypeError("each opaque payload must be bytes")
    payload_bytes = sum(map(len, payloads))
    if payload_bytes > MAX_PAYLOAD_TOTAL_BYTES:
        raise ValueError("payload total exceeds the 256 MiB safety limit")
    _strict_int(shard, 0, (1 << 32) - 1)
    _strict_int(epoch, 0, (1 << 64) - 1)
    path = Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError("case directory must be new or empty")
    path.mkdir(parents=True, exist_ok=True)
    _fsync_dir(path.parent)
    for name in ("verifier", "provider", "setup"):
        (path / name).mkdir(mode=0o700 if name == "setup" else 0o755)
    _fsync_dir(path)
    owner = path / ("verifier" if mode == "archive" else "provider")
    (owner / "blocks").mkdir()
    _fsync_dir(owner)

    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    leaves = []
    for position, payload in enumerate(payloads, 1):
        
        
        timestamp = (1700000000000000000 + epoch * 1000000 + position) % (1 << 64)
        block = _CANONICAL_HEADER.pack(
            CANONICAL_MAGIC, CANONICAL_VERSION, shard, epoch, position,
            timestamp, len(payload)) + payload
        _atomic_write(owner / "blocks" / f"{position:08d}.bin", block)
        leaves.append(leaf_hash((shard, epoch, position), block))
    tree = build_merkle(leaves)
    tree_ready_ns = time.perf_counter_ns() - started
    seed = secrets.token_bytes(32)
    backend = _BACKEND
    public = backend.public_from_seed(seed)
    signature = backend.sign(seed, root_statement(shard, epoch, epoch, tree.root))
    if not backend.verify(public, root_statement(shard, epoch, epoch, tree.root), signature):
        raise RuntimeError("setup certificate failed its own signature verification")
    _atomic_write(path / "setup/signing_seed.bin", seed, private=True)
    certificate = {"shard": shard, "epoch": epoch, "k": epoch,
                   "root_hex": tree.root.hex(), "signature_hex": signature.hex()}
    _atomic_write(owner / "certificate.json", _json_bytes(certificate))
    support_kind = "none"
    if mode in ("archive", "full", "ext_cached"):
        support_kind = "full_levels"
        _atomic_write(owner / "levels.bin", b"".join(
            digest for level in tree.levels for digest in level))
        geometry = {"format": "msi-pi-support", "version": 1,
                    "kind": support_kind, "digest_bytes": 32,
                    "level_counts": [len(level) for level in tree.levels],
                    "order": "leaves_to_root", "padding": "repeat_last"}
        _atomic_write(owner / "support.json", _json_bytes(geometry))
    elif mode == "leaf":
        support_kind = "padded_leaves"
        _atomic_write(owner / "leaves.bin", b"".join(tree.padded_leaves))
        geometry = {"format": "msi-pi-support", "version": 1,
                    "kind": support_kind, "digest_bytes": 32,
                    "leaf_count": len(tree.padded_leaves), "padding": "repeat_last"}
        _atomic_write(owner / "support.json", _json_bytes(geometry))
    common = {"version": 1, "mode": mode, "layout": "per_block", "codec": "raw-v1",
              "shard": shard, "epoch": epoch, "n": len(payloads),
              "n_prime": len(tree.padded_leaves), "k": epoch,
              "max_block_bytes": max(map(len, payloads)) + CANONICAL_HEADER_BYTES,
              "payload_bytes": payload_bytes,
              "canonical_bytes": payload_bytes + len(payloads) * CANONICAL_HEADER_BYTES,
              "root_hex": tree.root.hex(), "support_kind": support_kind}
    _atomic_write(owner / "store.json", _json_bytes({"format": "msi-pi-store", **common}))
    descriptor = {"format": "msi-pi-case", **common, "public_key_hex": public.hex(),
                  "canonical_envelope": "original-E2-MSB2-v1",
                  "payload_interpretation": "opaque bytes; not a native Ethereum root",
                  "hash_algorithm": "original E2 domain-separated SHA-256",
                  "padding": "repeat_last", "certificate_profile": "original-E2-direct-Ed25519",
                  "paper_descriptor_bytes": PAPER_DESCRIPTOR_BYTES,
                  "setup_tree_and_payload_write_ns": tree_ready_ns,
                  "setup_build_ns": time.perf_counter_ns() - started,
                  "setup_cpu_ns": time.process_time_ns() - cpu_started,
                  "setup_time_scope": "payload writes, tree construction, signer, certificate and support persistence; excludes final descriptor publication"}
    
    _atomic_write(path / "verifier/descriptor.json", _json_bytes(descriptor))
    _fsync_dir(path)
    _trusted_verifier(public)
    return descriptor


class _PackedLevel:
    
    def __init__(self, stream: BinaryIO, offset: int, count: int, stats: dict):
        self.stream, self.offset, self.count, self.stats = stream, offset, count, stats

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> bytes:
        if not 0 <= index < self.count:
            raise IndexError(index)
        self.stream.seek(self.offset + index * 32)
        digest = self.stream.read(32)
        self.stats["support_read_bytes"] += len(digest)
        self.stats["support_read_operations"] += 1
        if len(digest) != 32:
            raise ValueError("truncated full-tree support")
        return digest


def query_store(path: Path, position: int) -> dict:
    





    start = time.perf_counter_ns()
    cpu_start = time.thread_time_ns()
    path = Path(path)
    if not (path / "verifier/descriptor.json").is_file():
        raise ValueError("case has no committed trusted descriptor")
    owner = path / "provider"
    if not (owner / "store.json").exists():
        owner = path / "verifier"
    store = _read_json(owner / "store.json")
    if store.get("format") != "msi-pi-store":
        raise ValueError("invalid provider manifest")
    shard, epoch, n, n_prime = _context(store)
    _strict_int(position, 1, n)
    mode = store["mode"]
    if (mode == "archive") != (owner.name == "verifier"):
        raise ValueError("manifest owner and mode disagree")
    stats = {"payload_read_ns": 0, "witness_generate_ns": 0,
             "payload_read_bytes": 0, "payload_read_operations": 0,
             "support_read_bytes": 0, "support_read_operations": 0,
             "metadata_read_bytes": (owner / "store.json").stat().st_size,
             "support_bytes": 0, "mode": mode,
             "cpu_clock": "thread_time_ns", "io_scope": "application logical reads; OS caches enabled",
             "rebuilds_tree_each_query": mode == "ext_rebuild"}

    def read_block(pos: int) -> bytes:
        tick = time.perf_counter_ns()
        with (owner / "blocks" / f"{pos:08d}.bin").open("rb") as stream:
            block = stream.read(store["max_block_bytes"] + 1)
        stats["payload_read_ns"] += time.perf_counter_ns() - tick
        stats["payload_read_bytes"] += len(block)
        stats["payload_read_operations"] += 1
        if len(block) > store["max_block_bytes"]:
            raise ValueError("stored block exceeds configured size")
        if parse_canonical_block(block, (shard, epoch, pos)) is None:
            raise ValueError("stored block has an invalid canonical envelope")
        return block

    if mode == "ext_rebuild":
        tick = time.perf_counter_ns()
        leaves = []
        block = b""
        for pos in range(1, n + 1):
            candidate = read_block(pos)
            leaves.append(leaf_hash((shard, epoch, pos), candidate))
            if pos == position:
                block = candidate
        tree = build_merkle(leaves)
        merkle_path = extract_path(tree.levels, position)
        
        
        stats["witness_generate_ns"] = time.perf_counter_ns() - tick - stats["payload_read_ns"]
        del tree, leaves
    else:
        block = read_block(position)
        tick = time.perf_counter_ns()
        geometry = _read_json(owner / "support.json")
        stats["metadata_read_bytes"] += (owner / "support.json").stat().st_size
        if (geometry.get("format") != "msi-pi-support" or geometry.get("version") != 1
                or geometry.get("digest_bytes") != 32 or geometry.get("padding") != "repeat_last"):
            raise ValueError("invalid support geometry")
        if mode == "leaf":
            expected_size = n_prime * 32
            if geometry.get("kind") != "padded_leaves" or geometry.get("leaf_count") != n_prime:
                raise ValueError("invalid leaf support geometry")
            with (owner / "leaves.bin").open("rb") as stream:
                raw = stream.read(expected_size + 1)
            if len(raw) != expected_size:
                raise ValueError("leaf support length does not match trusted geometry")
            stats["support_bytes"] = expected_size
            stats["support_read_bytes"] = expected_size
            stats["support_read_operations"] = 1
            vector = tuple(raw[i:i + 32] for i in range(0, len(raw), 32))
            if any(digest != vector[n - 1] for digest in vector[n:]):
                raise ValueError("padded leaf vector must repeat the last real leaf")
            
            
            merkle_path = None
        else:
            counts = [n_prime >> depth for depth in range(n_prime.bit_length())]
            if (geometry.get("kind") != "full_levels" or geometry.get("level_counts") != counts
                    or geometry.get("order") != "leaves_to_root"):
                raise ValueError("invalid full support geometry")
            expected_size = (2 * n_prime - 1) * 32
            stats["support_bytes"] = expected_size
            with (owner / "levels.bin").open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != expected_size:
                    raise ValueError("full support length disagrees with geometry")
                levels, offset = [], 0
                for count in counts:
                    levels.append(_PackedLevel(stream, offset, count, stats))
                    offset += count * 32
                merkle_path = extract_path(tuple(levels), position)
        stats["witness_generate_ns"] = time.perf_counter_ns() - tick
    certificate = _read_json(owner / "certificate.json")
    stats["metadata_read_bytes"] += (owner / "certificate.json").stat().st_size
    response = {"format": "msi-pi-response", "version": 1,
                "query": [shard, epoch, position], "n": n, "n_prime": n_prime,
                "layout": "per_block", "codec": "raw-v1", "mode": mode,
                "block_b64": base64.b64encode(block).decode("ascii"),
                "leaf_hex": leaf_hash((shard, epoch, position), block).hex(),
                "certificate": certificate}
    if mode == "leaf":
        response.update(witness_kind="leaf", leaves_hex=[digest.hex() for digest in vector])
    else:
        response.update(witness_kind="path", path=[
            {"digest_hex": digest.hex(), "direction": direction}
            for digest, direction in merkle_path])
    stats["logical_read_bytes"] = (stats["payload_read_bytes"] + stats["support_read_bytes"]
                                    + stats["metadata_read_bytes"])
    stats["provider_cpu_ns"] = time.thread_time_ns() - cpu_start
    stats["provider_total_ns"] = time.perf_counter_ns() - start
    return {"response": response, "provider": stats}


def verify_response(descriptor: dict, response: dict) -> bool:
    




    try:
        if descriptor.get("format") != "msi-pi-case":
            return False
        shard, epoch, n, n_prime = _context(descriptor)
        root, public = _digest(descriptor["root_hex"]), _digest(descriptor["public_key_hex"])
        expected_keys = {"format", "version", "query", "n", "n_prime", "layout", "codec",
                         "block_b64", "leaf_hex", "mode", "witness_kind", "certificate"}
        expected_keys.add("leaves_hex" if descriptor["mode"] == "leaf" else "path")
        if not isinstance(response, dict) or set(response) != expected_keys:
            return False
        if (response["format"] != "msi-pi-response" or type(response["version"]) is not int
                or response["version"] != 1 or response["layout"] != descriptor["layout"]
                or response["codec"] != descriptor["codec"]
                or response["mode"] != descriptor["mode"]):
            return False
        if (_strict_int(response["n"], 1, MAX_N) != n
                or _strict_int(response["n_prime"], 1, MAX_N) != n_prime):
            return False
        q = response["query"]
        if not isinstance(q, list) or len(q) != 3:
            return False
        if (_strict_int(q[0], 0, (1 << 32) - 1) != shard
                or _strict_int(q[1], 0, (1 << 64) - 1) != epoch):
            return False
        position = _strict_int(q[2], 1, n)
        encoded = response["block_b64"]
        if not isinstance(encoded, str) or len(encoded) > ((descriptor["max_block_bytes"] + 2) // 3) * 4:
            return False
        block = base64.b64decode(encoded, validate=True)
        if len(block) > descriptor["max_block_bytes"]:
            return False
        query = (shard, epoch, position)
        if parse_canonical_block(block, query) is None:
            return False
        queried_leaf = leaf_hash(query, block)
        if _digest(response["leaf_hex"]) != queried_leaf:
            return False
        if descriptor["mode"] == "leaf":
            if response["witness_kind"] != "leaf":
                return False
            encoded_leaves = response["leaves_hex"]
            if not isinstance(encoded_leaves, list) or len(encoded_leaves) != n_prime:
                return False
            vector = tuple(_digest(item) for item in encoded_leaves)
            if vector[position - 1] != queried_leaf:
                return False
            if any(digest != vector[n - 1] for digest in vector[n:]):
                return False
            decoded_path = rebuild_path(position, vector)
            if decoded_path is None:
                return False
        else:
            if response["witness_kind"] != "path":
                return False
            path = response["path"]
            if not isinstance(path, list) or len(path) != n_prime.bit_length() - 1:
                return False
            decoded_path = []
            for depth, item in enumerate(path):
                if not isinstance(item, dict) or set(item) != {"digest_hex", "direction"}:
                    return False
                direction = _strict_int(item["direction"], 0, 1)
                if direction != (((position - 1) >> depth) & 1):
                    return False
                decoded_path.append((_digest(item["digest_hex"]), direction))
        cert = response["certificate"]
        if not isinstance(cert, dict) or set(cert) != {"shard", "epoch", "k", "root_hex", "signature_hex"}:
            return False
        if (_strict_int(cert["shard"], 0, (1 << 32) - 1) != shard
                or _strict_int(cert["epoch"], 0, (1 << 64) - 1) != epoch
                or _strict_int(cert["k"], 0, (1 << 64) - 1) != descriptor["k"]
                or _digest(cert["root_hex"]) != root):
            return False
        signature = _digest(cert["signature_hex"], 64)
        if not verify_member(query, block, tuple(decoded_path), root, n):
            return False
        return bool(_trusted_verifier(public).verify(
            root_statement(shard, epoch, descriptor["k"], root), signature))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError,
            binascii.Error, OSError, RuntimeError):
        return False


def storage_accounting(path: Path) -> dict:
    





    path = Path(path)
    descriptor = load_descriptor(path)
    result = {"mode": descriptor["mode"], "n": descriptor["n"],
              "n_prime": descriptor["n_prime"], "paper_descriptor_bytes": PAPER_DESCRIPTOR_BYTES,
              "descriptor_serialized_bytes": (path / "verifier/descriptor.json").stat().st_size,
              "certificate_bytes": 0, "support_metadata_bytes": 0,
              "provider_internal_cached_support_bytes": 0,
              "accounting_scope": "regular file lengths; allocated file blocks also reported; excludes directories, inodes, journal, and OS cache"}
    for role in ("verifier", "provider", "setup"):
        root = path / role
        files = [entry for entry in root.rglob("*") if entry.is_file()]
        for entry in files:
            if entry.is_symlink():
                raise ValueError("storage accounting does not follow symlink artifacts")
        result[f"{role}_total_bytes"] = sum(entry.stat().st_size for entry in files)
        result[f"{role}_allocated_bytes"] = sum(entry.stat().st_blocks * 512 for entry in files)
        result[f"{role}_file_count"] = len(files)
        result[f"{role}_payload_bytes"] = sum(entry.stat().st_size for entry in files if entry.parent.name == "blocks")
        result[f"{role}_support_bytes"] = sum(entry.stat().st_size for entry in files if entry.name in ("levels.bin", "leaves.bin"))
        result["certificate_bytes"] += sum(entry.stat().st_size for entry in files if entry.name == "certificate.json")
        result["support_metadata_bytes"] += sum(entry.stat().st_size for entry in files if entry.name == "support.json")
    result["setup_signing_key_bytes"] = (path / "setup/signing_seed.bin").stat().st_size
    if descriptor["mode"] == "ext_cached":
        result["provider_internal_cached_support_bytes"] = result["provider_support_bytes"]
    result["total_materialized_bytes"] = sum(result[f"{role}_total_bytes"] for role in ("verifier", "provider", "setup"))
    result["operational_total_bytes"] = result["verifier_total_bytes"] + result["provider_total_bytes"]
    result["raw_opaque_payload_bytes"] = descriptor["payload_bytes"]
    result["canonical_envelope_overhead_bytes"] = descriptor["n"] * CANONICAL_HEADER_BYTES
    return result
