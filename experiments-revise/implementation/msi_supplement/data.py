
























from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


MAX_ETHEREUM_BLOCKS = 10_000
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_RECORD_BYTES = 4 * ((MAX_PAYLOAD_BYTES + 2) // 3) + 65_536
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")


class DatasetError(ValueError):
    pass


def _positive_int(value: Any, name: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        bound = f"an integer between 1 and {maximum}" if maximum is not None else "a positive integer"
        raise DatasetError(f"{name} must be {bound}")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise DatasetError(f"{name} must be a nonnegative integer")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError("JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DatasetError("JSON contains a nonfinite number")


def _json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise DatasetError(f"Invalid {label} JSON") from None


def _check_destination(output: Path) -> Path:
    output = Path(output)
    if output.is_symlink() or (output.exists() and
                              (not output.is_dir() or any(output.iterdir()))):
        raise DatasetError("Dataset destination must be absent or an empty directory")
    return output


def _write_dataset(output: Path, manifest: dict[str, Any],
                   records: list[dict[str, Any]]) -> Path:
    output = _check_destination(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dataset-", dir=output.parent) as temporary:
        staging = Path(temporary) / "ready"
        staging.mkdir()
        file_hash = hashlib.sha256()
        with (staging / "blocks.jsonl").open("wb") as handle:
            for record in records:
                line = _canonical(record) + b"\n"
                handle.write(line)
                file_hash.update(line)
        manifest.update({"format_version": 1, "native_finality_verified": False,
                         "block_count": len(records),
                         "total_payload_bytes": sum(r["size_bytes"] for r in records),
                         "blocks_sha256": file_hash.hexdigest()})
        (staging / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        _check_destination(output)
        
        staging.replace(output)
    return output


def _record(index: int, payload: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise DatasetError("Payload exceeds the 64 MiB per-block limit")
    return {"index": index, "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), "metadata": metadata,
            "payload_b64": base64.b64encode(payload).decode("ascii")}


def create_synthetic(output: Path, epochs: int, blocks_per_epoch: int,
                     payload_bytes: int, seed: int = 2026) -> Path:
    




    output = _check_destination(output)
    _positive_int(epochs, "epochs")
    _positive_int(blocks_per_epoch, "blocks_per_epoch")
    _positive_int(payload_bytes, "payload_bytes", MAX_PAYLOAD_BYTES)
    if type(seed) is not int:
        raise DatasetError("seed must be an integer")
    records = []
    for index in range(epochs * blocks_per_epoch):
        domain = _canonical(["msi-supplement-synthetic-v1", seed, index])
        payload = b"".join(hashlib.sha256(domain + counter.to_bytes(8, "big")).digest()
                           for counter in range((payload_bytes + 31) // 32))[:payload_bytes]
        records.append(_record(index, payload, {"epoch": index // blocks_per_epoch,
                                                "block_in_epoch": index % blocks_per_epoch}))
    return _write_dataset(output, {
        "data_kind": "synthetic", "epochs": epochs, "blocks_per_epoch": blocks_per_epoch,
        "payload_bytes": payload_bytes, "seed": seed,
        "provenance": {"generator": "sha256-counter-v1", "source": "synthetic"},
    }, records)


def _quantity(value: Any, label: str) -> int:
    if not isinstance(value, str) or not _QUANTITY.fullmatch(value):
        raise DatasetError(f"Malformed {label} hexadecimal quantity")
    return int(value, 16)


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise DatasetError(f"Malformed {label} hash")
    return value.lower()


def _block_metadata(block: Any, expected_number: int | None = None,
                    full_transactions: bool = False) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise DatasetError("RPC block is missing or is not an object")
    number = _quantity(block.get("number"), "block number")
    if expected_number is not None and number != expected_number:
        raise DatasetError("RPC block height does not match the requested height")
    block_hash = _hash(block.get("hash"), "block")
    parent_hash = _hash(block.get("parentHash"), "parent")
    if number == 0 and parent_hash != "0x" + "0" * 64:
        raise DatasetError("Genesis block parent hash must be zero")
    if full_transactions:
        transactions = block.get("transactions")
        if not isinstance(transactions, list):
            raise DatasetError("RPC full block has no transaction list")
        transaction_hashes = set()
        for index, transaction in enumerate(transactions):
            if not isinstance(transaction, dict):
                raise DatasetError("RPC did not return full transaction objects")
            tx_hash = _hash(transaction.get("hash"), "transaction")
            if tx_hash in transaction_hashes:
                raise DatasetError("RPC block contains duplicate transaction hashes")
            transaction_hashes.add(tx_hash)
            if (_hash(transaction.get("blockHash"), "transaction block") != block_hash or
                    _quantity(transaction.get("blockNumber"), "transaction block number") != number or
                    _quantity(transaction.get("transactionIndex"), "transaction index") != index):
                raise DatasetError("RPC transaction association does not match its block")
    return {"number": number, "hash": block_hash, "parentHash": parent_hash}


class _Rpc:
    def __init__(self, url: str, timeout: float):
        if not isinstance(url, str) or any(ord(character) <= 32 for character in url):
            raise DatasetError("RPC URL must be a string without whitespace or control characters")
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.fragment
            parsed.port
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise DatasetError("RPC URL must be an HTTP(S) endpoint without a fragment")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise DatasetError("timeout must be greater than zero and at most 300 seconds")
        self.url, self.timeout, self.request_id = url, timeout, 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        body = _canonical({"jsonrpc": "2.0", "id": self.request_id,
                           "method": method, "params": params})
        for attempt in range(3):
            request = urllib.request.Request(self.url, data=body, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_PAYLOAD_BYTES + 1)
                if len(raw) > MAX_PAYLOAD_BYTES:
                    raise DatasetError("RPC response exceeds the 64 MiB limit")
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise DatasetError(f"RPC HTTP request failed with status {status}") from None
            except (urllib.error.URLError, TimeoutError, OSError, HTTPException, UnicodeError):
                if attempt == 2:
                    raise DatasetError("RPC transport failed after three attempts") from None
            time.sleep(0.1 * (2 ** attempt))
        message = _json(raw, "RPC response")
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or
                type(message.get("id")) is not int or message["id"] != self.request_id or
                "error" in message or "result" not in message):
            
            raise DatasetError("RPC returned an error or malformed JSON-RPC response")
        return message["result"]


def fetch_ethereum(output: Path, rpc_url: str, block_count: int,
                   start_block: int | None = None, timeout: float = 30) -> Path:
    








    output = _check_destination(output)
    _positive_int(block_count, "block_count", MAX_ETHEREUM_BLOCKS)
    if start_block is not None:
        _nonnegative_int(start_block, "start_block")
    rpc = _Rpc(rpc_url, timeout)
    chain_id = _quantity(rpc.call("eth_chainId", []), "chain ID")
    finalized = _block_metadata(rpc.call("eth_getBlockByNumber", ["finalized", False]))
    start = finalized["number"] - block_count + 1 if start_block is None else start_block
    if start < 0 or start + block_count - 1 > finalized["number"]:
        raise DatasetError("Requested range is outside the initial RPC-finalized history")
    records: list[dict[str, Any]] = []
    previous_hash = None
    for index, number in enumerate(range(start, start + block_count)):
        block = rpc.call("eth_getBlockByNumber", [hex(number), True])
        metadata = _block_metadata(block, number, full_transactions=True)
        if previous_hash is not None and metadata["parentHash"] != previous_hash:
            raise DatasetError("RPC blocks have a broken contiguous parent-hash link")
        if number == finalized["number"] and metadata["hash"] != finalized["hash"]:
            raise DatasetError("RPC finalized block changed during acquisition")
        previous_hash = metadata["hash"]
        records.append(_record(index, _canonical(block), metadata))
    if _quantity(rpc.call("eth_chainId", []), "chain ID") != chain_id:
        raise DatasetError("RPC chain ID changed during acquisition")
    finalized_after = _block_metadata(rpc.call("eth_getBlockByNumber", ["finalized", False]))
    if (finalized_after["number"] < finalized["number"] or
            (finalized_after["number"] == finalized["number"] and finalized_after != finalized)):
        raise DatasetError("RPC finalized head regressed or changed during acquisition")
    original_head = _block_metadata(rpc.call("eth_getBlockByNumber",
                                            [hex(finalized["number"]), False]), finalized["number"])
    if original_head != finalized:
        raise DatasetError("RPC original finalized block changed during acquisition")
    for index in sorted({0, block_count - 1}):
        number = records[index]["metadata"]["number"]
        check = rpc.call("eth_getBlockByNumber", [hex(number), True])
        _block_metadata(check, number, full_transactions=True)
        if hashlib.sha256(_canonical(check)).hexdigest() != records[index]["sha256"]:
            raise DatasetError("RPC boundary block snapshot changed during acquisition")
    return _write_dataset(output, {
        "data_kind": "ethereum_rpc_finalized_json", "chain_id": chain_id,
        "start_block": start, "end_block": start + block_count - 1,
        "provenance": {
            "source": "user-supplied Ethereum JSON-RPC endpoint; URL omitted",
            "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
            "rpc_methods": ["eth_chainId", "eth_getBlockByNumber"],
            "full_transactions": True, "payload_encoding": "canonical-json-utf8",
            "finality_basis": "trusted RPC finalized tag; no native finality verification",
            "initial_finalized_head": finalized, "finalized_head_after": finalized_after,
            "chain_id_rechecked": True, "boundary_snapshots_rechecked": True,
        },
    }, records)


def load_dataset(path: Path) -> tuple[dict[str, Any], list[bytes]]:
    






    path = Path(path)
    if path.name == "manifest.json" and path.is_file():
        path = path.parent
    try:
        manifest_path = path / "manifest.json"
        if manifest_path.stat().st_size > 1024 * 1024:
            raise DatasetError("Manifest exceeds the 1 MiB limit")
        manifest = _json(manifest_path.read_bytes(), "manifest")
    except OSError:
        raise DatasetError("Cannot read dataset manifest") from None
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
        raise DatasetError("Unsupported dataset format_version")
    kind = manifest.get("data_kind")
    if kind not in {"synthetic", "ethereum_rpc_finalized_json"}:
        raise DatasetError("Unsupported dataset data_kind")
    if manifest.get("native_finality_verified") is not False:
        raise DatasetError("Dataset must explicitly state native_finality_verified=false")
    count = _positive_int(manifest.get("block_count"), "block_count",
                          MAX_ETHEREUM_BLOCKS if kind == "ethereum_rpc_finalized_json" else None)
    total_expected = _nonnegative_int(manifest.get("total_payload_bytes"), "total_payload_bytes")
    digest = manifest.get("blocks_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise DatasetError("Malformed blocks file SHA-256")
    if kind == "synthetic":
        epochs = _positive_int(manifest.get("epochs"), "epochs")
        blocks_per_epoch = _positive_int(manifest.get("blocks_per_epoch"), "blocks_per_epoch")
        payload_size = _positive_int(manifest.get("payload_bytes"), "payload_bytes", MAX_PAYLOAD_BYTES)
        if epochs * blocks_per_epoch != count or type(manifest.get("seed")) is not int:
            raise DatasetError("Synthetic manifest dimensions or seed are invalid")
    else:
        _nonnegative_int(manifest.get("chain_id"), "chain_id")
        start = _nonnegative_int(manifest.get("start_block"), "start_block")
        end = _nonnegative_int(manifest.get("end_block"), "end_block")
        provenance = manifest.get("provenance")
        if not isinstance(provenance, dict):
            raise DatasetError("Missing Ethereum provenance")
        initial = _validate_saved_head(provenance.get("initial_finalized_head"))
        final = _validate_saved_head(provenance.get("finalized_head_after"))
        if (end != start + count - 1 or end > initial["number"] or
                final["number"] < initial["number"] or
                (final["number"] == initial["number"] and final != initial)):
            raise DatasetError("Invalid recorded RPC-finalized block range")
    payloads: list[bytes] = []
    file_hash = hashlib.sha256()
    total = 0
    previous_hash = None
    try:
        with (path / "blocks.jsonl").open("rb") as handle:
            while True:
                line = handle.readline(_MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_RECORD_BYTES or not line.endswith(b"\n"):
                    raise DatasetError("Oversized or unterminated dataset record")
                index = len(payloads)
                if index >= count:
                    raise DatasetError("Dataset has more records than declared")
                file_hash.update(line)
                record = _json(line, "record")
                if not isinstance(record, dict) or type(record.get("index")) is not int or record["index"] != index:
                    raise DatasetError("Dataset record indexes are not contiguous")
                size = _nonnegative_int(record.get("size_bytes"), "record size_bytes")
                if size > MAX_PAYLOAD_BYTES:
                    raise DatasetError("Payload exceeds the 64 MiB per-block limit")
                try:
                    payload = base64.b64decode(record["payload_b64"], validate=True)
                except (KeyError, TypeError, ValueError, binascii.Error):
                    raise DatasetError("Invalid record base64 payload") from None
                if size != len(payload) or record.get("sha256") != hashlib.sha256(payload).hexdigest():
                    raise DatasetError("Dataset payload size or SHA-256 mismatch")
                metadata = record.get("metadata")
                if not isinstance(metadata, dict):
                    raise DatasetError("Missing dataset record metadata")
                if kind == "synthetic":
                    if (size != payload_size or metadata != {
                            "epoch": index // blocks_per_epoch,
                            "block_in_epoch": index % blocks_per_epoch}):
                        raise DatasetError("Synthetic record dimensions or metadata mismatch")
                else:
                    block = _json(payload, "block payload")
                    expected_metadata = _block_metadata(block, start + index, full_transactions=True)
                    if payload != _canonical(block) or metadata != expected_metadata:
                        raise DatasetError("Ethereum payload is not canonical or its metadata differs")
                    if previous_hash is not None and metadata["parentHash"] != previous_hash:
                        raise DatasetError("Dataset has a broken contiguous parent-hash link")
                    if metadata["number"] == initial["number"] and metadata != initial:
                        raise DatasetError("Dataset block differs from the recorded finalized head")
                    previous_hash = metadata["hash"]
                total += size
                payloads.append(payload)
    except OSError:
        raise DatasetError("Cannot read dataset block records") from None
    if len(payloads) != count or total != total_expected or file_hash.hexdigest() != digest:
        raise DatasetError("Dataset record count, total size, or blocks file SHA-256 mismatch")
    return manifest, payloads


def _validate_saved_head(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetError("Missing recorded RPC-finalized head")
    number = _nonnegative_int(value.get("number"), "recorded finalized number")
    result = {"number": number, "hash": _hash(value.get("hash"), "recorded finalized"),
              "parentHash": _hash(value.get("parentHash"), "recorded finalized parent")}
    if result != value:
        raise DatasetError("Malformed recorded RPC-finalized head")
    return result
