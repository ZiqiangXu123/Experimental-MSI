

import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from msi_supplement.data import DatasetError, create_synthetic, fetch_ethereum, load_dataset


def block_hash(number):
    return "0x" + f"{number + 1:064x}"


def block(number, full=True):
    transaction = {
        "hash": "0x" + f"{number + 100:064x}", "blockHash": block_hash(number),
        "blockNumber": hex(number), "transactionIndex": "0x0", "input": "0x1234",
        "from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "value": "0x0",
    }
    return {
        "number": hex(number), "hash": block_hash(number),
        "parentHash": block_hash(number - 1), "timestamp": hex(1700000000 + number * 12),
        "transactions": [transaction if full else transaction["hash"]],
    }


@contextmanager
def fake_rpc(transform=None, finalized=12):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request)
            method, params = request["method"], request["params"]
            if method == "eth_chainId":
                result = "0x1"
            elif method == "eth_getBlockByNumber":
                result = block(finalized if params[0] == "finalized" else int(params[0], 16), params[1])
            else:
                raise AssertionError("Unexpected RPC method")
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            status = 200
            if transform is not None:
                transformed = transform(request, response, calls)
                if isinstance(transformed, tuple):
                    status, response = transformed
                elif transformed is not None:
                    response = transformed
            raw = response if isinstance(response, bytes) else json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/secret-path?key=VERY_SECRET", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "dataset"

    def tearDown(self):
        self.temporary.cleanup()

    def synthetic(self):
        return create_synthetic(self.output, 2, 3, 47)

    def rewrite(self, change_record=None, change_manifest=None):
        manifest = json.loads((self.output / "manifest.json").read_text())
        records = [json.loads(line) for line in (self.output / "blocks.jsonl").read_text().splitlines()]
        if change_record:
            change_record(records)
        raw = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                       for row in records)
        (self.output / "blocks.jsonl").write_bytes(raw)
        manifest["blocks_sha256"] = hashlib.sha256(raw).hexdigest()
        if change_manifest:
            change_manifest(manifest)
        (self.output / "manifest.json").write_text(json.dumps(manifest))

    def test_synthetic_deterministic_and_roundtrip(self):
        self.synthetic()
        other = create_synthetic(self.root / "other", 2, 3, 47)
        for name in ("manifest.json", "blocks.jsonl"):
            self.assertEqual((self.output / name).read_bytes(), (other / name).read_bytes())
        manifest, payloads = load_dataset(self.output / "manifest.json")
        self.assertEqual(manifest["data_kind"], "synthetic")
        self.assertIs(manifest["native_finality_verified"], False)
        self.assertEqual(manifest["total_payload_bytes"], 282)
        self.assertEqual([len(value) for value in payloads], [47] * 6)
        self.assertEqual(len(set(payloads)), 6)

    def test_seed_changes_payloads(self):
        self.synthetic()
        other = create_synthetic(self.root / "other", 2, 3, 47, seed=2027)
        self.assertNotEqual(load_dataset(self.output)[1], load_dataset(other)[1])

    def test_refuses_to_overwrite_nonempty_destination(self):
        self.synthetic()
        before = (self.output / "manifest.json").read_bytes()
        with self.assertRaisesRegex(DatasetError, "empty directory"):
            self.synthetic()
        with self.assertRaisesRegex(DatasetError, "empty directory"):
            fetch_ethereum(self.output, "http://unused.invalid", 3)
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_existing_empty_directory_is_allowed(self):
        self.output.mkdir()
        self.synthetic()
        self.assertEqual(len(load_dataset(self.output)[1]), 6)

    def test_invalid_creator_arguments(self):
        for epochs, blocks, size, seed in [(0, 1, 1, 2), (1, -1, 1, 2), (1, 1, 0, 2),
                                           (True, 1, 1, 2), (1, 1, 1, 1.5),
                                           (1, 1, 65 * 1024 * 1024, 2)]:
            with self.subTest(arguments=(epochs, blocks, size, seed)), self.assertRaises(DatasetError):
                create_synthetic(self.output, epochs, blocks, size, seed)
        for count, start, timeout in [(10001, None, 30), (0, None, 30), (1, -1, 30),
                                      (1, None, 0), (1, None, float("nan")), (1, None, 301)]:
            with self.subTest(arguments=(count, start, timeout)), self.assertRaises(DatasetError):
                fetch_ethereum(self.output, "http://unused.invalid", count, start, timeout)
        with self.assertRaises(DatasetError):
            fetch_ethereum(self.output, "file:///tmp/secret", 1)

    def test_payload_hash_validation_even_with_updated_file_hash(self):
        self.synthetic()
        self.rewrite(lambda records: records[0].update(payload_b64=base64.b64encode(b"X" * 47).decode()))
        with self.assertRaisesRegex(DatasetError, "SHA-256 mismatch"):
            load_dataset(self.output)

    def test_file_hash_detects_unrecorded_changes(self):
        self.synthetic()
        with (self.output / "blocks.jsonl").open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaises(DatasetError):
            load_dataset(self.output)

    def test_record_size_base64_and_index_validation(self):
        for field, value in [("size_bytes", 0), ("payload_b64", "!"), ("index", 9),
                             ("metadata", {}), ("payload_b64", [1, 2])]:
            with self.subTest(field=field, value=value):
                destination = self.root / field / str(len(list(self.root.iterdir())))
                self.output = destination
                self.synthetic()
                self.rewrite(lambda records: records[0].update({field: value}))
                with self.assertRaises(DatasetError):
                    load_dataset(self.output)

    def test_manifest_rejects_false_finality_claim_and_bad_count(self):
        for field, value in [("native_finality_verified", True), ("format_version", True),
                             ("block_count", 4), ("data_kind", "unknown"), ("total_payload_bytes", 1)]:
            with self.subTest(field=field):
                self.output = self.root / field
                self.synthetic()
                self.rewrite(change_manifest=lambda manifest: manifest.update({field: value}))
                with self.assertRaises(DatasetError):
                    load_dataset(self.output)

    def test_malformed_manifest_and_missing_file(self):
        with self.assertRaises(DatasetError):
            load_dataset(self.output)
        self.output.mkdir()
        (self.output / "manifest.json").write_text('{"format_version":1,"format_version":1}')
        with self.assertRaises(DatasetError):
            load_dataset(self.output)

    def test_ethereum_full_blocks_roundtrip_and_provenance(self):
        with fake_rpc() as (url, calls):
            fetch_ethereum(self.output, url, 3, timeout=1)
        manifest, payloads = load_dataset(self.output)
        self.assertEqual((manifest["chain_id"], manifest["start_block"], manifest["end_block"]), (1, 10, 12))
        self.assertEqual(manifest["data_kind"], "ethereum_rpc_finalized_json")
        self.assertIs(manifest["native_finality_verified"], False)
        self.assertEqual([json.loads(value)["number"] for value in payloads], ["0xa", "0xb", "0xc"])
        self.assertEqual(sum(call["method"] == "eth_chainId" for call in calls), 2)
        self.assertEqual(sum(call["params"] == ["0xa", True] for call in calls), 2)
        self.assertEqual(sum(call["params"] == ["0xc", True] for call in calls), 2)
        recorded = (self.output / "manifest.json").read_text() + (self.output / "blocks.jsonl").read_text()
        for secret in ("VERY_SECRET", "secret-path", "127.0.0.1", url):
            self.assertNotIn(secret, recorded)

    def test_ethereum_explicit_start_and_genesis(self):
        with fake_rpc() as (url, _):
            fetch_ethereum(self.output, url, 2, start_block=0)
        manifest, payloads = load_dataset(self.output)
        self.assertEqual((manifest["start_block"], manifest["end_block"]), (0, 1))
        self.assertEqual(json.loads(payloads[0])["parentHash"], "0x" + "0" * 64)

    def test_rejects_unfinalized_or_negative_range_without_writing(self):
        for count, start in [(3, 11), (14, None)]:
            with self.subTest(count=count, start=start), fake_rpc() as (url, _):
                with self.assertRaisesRegex(DatasetError, "RPC-finalized history"):
                    fetch_ethereum(self.output, url, count, start)
                self.assertFalse(self.output.exists())

    def test_rejects_malformed_rpc_blocks(self):
        def make_transform(fault):
            def transform(request, response, calls):
                if request["params"] == ["0xb", True]:
                    fault(response["result"])
                return response
            return transform

        faults = [
            lambda value: value.update(hash="0xabc"),
            lambda value: value.update(parentHash=block_hash(3)),
            lambda value: value.update(number="0xc"),
            lambda value: value.update(number="0x00b"),
            lambda value: value.update(transactions=["0x" + "a" * 64]),
            lambda value: value["transactions"][0].update(blockNumber="0x1"),
            lambda value: value["transactions"][0].update(transactionIndex="0x1"),
            lambda value: value["transactions"][0].update(blockHash=block_hash(3)),
        ]
        for index, fault in enumerate(faults):
            with self.subTest(fault=index), fake_rpc(make_transform(fault)) as (url, _):
                with self.assertRaises(DatasetError):
                    fetch_ethereum(self.output, url, 3)
                self.assertFalse(self.output.exists())

    def test_rejects_rpc_finalized_tag_missing(self):
        def transform(request, response, calls):
            if request["params"] == ["finalized", False]:
                response["result"] = None
            return response
        with fake_rpc(transform) as (url, _), self.assertRaises(DatasetError):
            fetch_ethereum(self.output, url, 3)

    def test_rejects_chain_change(self):
        def transform(request, response, calls):
            if request["method"] == "eth_chainId" and len(calls) > 1:
                response["result"] = "0x2"
            return response
        with fake_rpc(transform) as (url, _), self.assertRaisesRegex(DatasetError, "chain ID changed"):
            fetch_ethereum(self.output, url, 3)

    def test_rejects_finalized_regression_or_mutation(self):
        for new_head in (block(11, False), {**block(12, False), "hash": block_hash(99)},
                         {**block(12, False), "parentHash": block_hash(99)}):
            def transform(request, response, calls):
                if request["params"] == ["finalized", False] and len(calls) > 2:
                    response["result"] = new_head
                return response
            with self.subTest(head=new_head), fake_rpc(transform) as (url, _):
                with self.assertRaisesRegex(DatasetError, "finalized head regressed or changed"):
                    fetch_ethereum(self.output, url, 3)

    def test_accepts_advancing_finalized_head(self):
        def transform(request, response, calls):
            if request["params"] == ["finalized", False] and len(calls) > 2:
                response["result"] = block(13, False)
            return response
        with fake_rpc(transform) as (url, _):
            fetch_ethereum(self.output, url, 3)
        self.assertEqual(load_dataset(self.output)[0]["provenance"]["finalized_head_after"]["number"], 13)

    def test_requeries_original_finalized_hash(self):
        def transform(request, response, calls):
            if request["params"] == ["0xc", False]:
                response["result"]["hash"] = block_hash(99)
            return response
        with fake_rpc(transform) as (url, _), self.assertRaisesRegex(DatasetError, "original finalized block changed"):
            fetch_ethereum(self.output, url, 3, start_block=1)

    def test_requeries_boundary_payload_not_just_hash(self):
        def transform(request, response, calls):
            if request["params"] == ["0xa", True] and sum(call["params"] == ["0xa", True] for call in calls) > 1:
                response["result"]["timestamp"] = "0x1"
            return response
        with fake_rpc(transform) as (url, _), self.assertRaisesRegex(DatasetError, "boundary block snapshot changed"):
            fetch_ethereum(self.output, url, 3)

    def test_retry_is_bounded_and_errors_do_not_echo_secrets(self):
        def transform(request, response, calls):
            return 503, {"error": "VERY_SECRET"}
        with fake_rpc(transform) as (url, calls):
            with self.assertRaises(DatasetError) as error:
                fetch_ethereum(self.output, url, 3)
            self.assertEqual(len(calls), 3)
            self.assertNotIn("VERY_SECRET", str(error.exception))
            self.assertNotIn(url, str(error.exception))

    def test_transient_rpc_failure_recovers(self):
        def transform(request, response, calls):
            if len(calls) == 1:
                return 429, {"error": "rate limited"}
            return response
        with fake_rpc(transform) as (url, calls):
            fetch_ethereum(self.output, url, 1)
        self.assertEqual(len(load_dataset(self.output)[1]), 1)
        self.assertEqual(calls[0]["id"], calls[1]["id"])

    def test_malformed_rpc_envelope_is_not_retried(self):
        bad = [b"not JSON", {"jsonrpc": "2.0", "id": 99, "result": "0x1"},
               {"jsonrpc": "2.0", "id": 1, "error": {"message": "VERY_SECRET"}},
               b'{"jsonrpc":"2.0","id":1,"result":"0x1","result":"0x2"}']
        for response in bad:
            with self.subTest(response=response), fake_rpc(lambda *_: response) as (url, calls):
                with self.assertRaises(DatasetError) as error:
                    fetch_ethereum(self.output, url, 3)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("VERY_SECRET", str(error.exception))

    def test_load_revalidates_ethereum_parent_link_after_rehashing(self):
        with fake_rpc() as (url, _):
            fetch_ethereum(self.output, url, 3)

        def change(records):
            value = json.loads(base64.b64decode(records[1]["payload_b64"]))
            value["parentHash"] = block_hash(1)
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            records[1].update(payload_b64=base64.b64encode(raw).decode(),
                              sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
            records[1]["metadata"]["parentHash"] = value["parentHash"]

        self.rewrite(change)
        with self.assertRaisesRegex(DatasetError, "parent-hash link"):
            load_dataset(self.output)

    def test_load_revalidates_finalized_range(self):
        with fake_rpc() as (url, _):
            fetch_ethereum(self.output, url, 3)
        self.rewrite(change_manifest=lambda manifest: manifest["provenance"]["initial_finalized_head"].update(number=11))
        with self.assertRaisesRegex(DatasetError, "RPC-finalized block range"):
            load_dataset(self.output)


if __name__ == "__main__":
    unittest.main()
