
from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SUITE = Path(__file__).resolve().parents[1]
if str(SUITE) not in sys.path:
    sys.path.insert(0, str(SUITE))

from msi_supplement.core import (
    MODES, create_case, load_descriptor, query_store, storage_accounting,
    verify_response,
)
from msi_query_exp.constants import CANONICAL_HEADER_BYTES
from msi_query_exp.crypto import Ed25519OpenSSL
from msi_query_exp.encoding import parse_canonical_block, root_statement
from msi_query_exp.merkle import build_merkle, leaf_hash


class CoreSupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="msi-core-tests-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_case(self, name="case", mode="full", n=3, shard=7, epoch=11):
        payloads = [json.dumps({"number": i, "opaque": "payload" + str(i)},
                               sort_keys=True, separators=(",", ":")).encode()
                    for i in range(n)]
        path = self.root / name
        descriptor = create_case(payloads, path, mode, shard=shard, epoch=epoch)
        return path, descriptor, payloads

    def test_all_modes_n_one_three_eight_equal_original_root(self):
        for n in (1, 3, 8):
            roots = set()
            for mode in MODES:
                with self.subTest(n=n, mode=mode):
                    path, descriptor, payloads = self.make_case(f"{mode}-{n}", mode, n)
                    roots.add(descriptor["root_hex"])
                    self.assertEqual(descriptor, load_descriptor(path))
                    blocks = []
                    for position in range(1, n + 1):
                        result = query_store(path, position)
                        response, stats = result["response"], result["provider"]
                        self.assertTrue(verify_response(descriptor, response))
                        block = base64.b64decode(response["block_b64"])
                        blocks.append(block)
                        self.assertEqual(block[CANONICAL_HEADER_BYTES:], payloads[position - 1])
                        self.assertIsNotNone(parse_canonical_block(block, (7, 11, position)))
                        for field in ("payload_read_ns", "witness_generate_ns", "provider_cpu_ns",
                                      "provider_total_ns", "payload_read_bytes", "support_read_bytes"):
                            self.assertGreaterEqual(stats[field], 0)
                        if mode == "leaf":
                            self.assertEqual(response["witness_kind"], "leaf")
                            self.assertNotIn("path", response)
                            self.assertEqual(len(response["leaves_hex"]), descriptor["n_prime"])
                        else:
                            self.assertEqual(response["witness_kind"], "path")
                    expected = build_merkle([leaf_hash((7, 11, i), b)
                                             for i, b in enumerate(blocks, 1)]).root.hex()
                    self.assertEqual(expected, descriptor["root_hex"])
            self.assertEqual(len(roots), 1)

    def test_storage_counts_include_cached_support_and_serialization(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                path, descriptor, _ = self.make_case(mode, mode)
                account = storage_accounting(path)
                owner = "verifier" if mode == "archive" else "provider"
                other = "provider" if mode == "archive" else "verifier"
                self.assertEqual(account[f"{owner}_payload_bytes"], descriptor["canonical_bytes"])
                self.assertEqual(account[f"{other}_payload_bytes"], 0)
                expected_support = 4 * 32 if mode == "leaf" else 7 * 32
                if mode == "ext_rebuild":
                    expected_support = 0
                self.assertEqual(account[f"{owner}_support_bytes"], expected_support)
                self.assertEqual(account["setup_signing_key_bytes"], 32)
                self.assertEqual(account["setup_total_bytes"], 32)
                self.assertGreater(account["descriptor_serialized_bytes"], account["paper_descriptor_bytes"])
                self.assertEqual(account["total_materialized_bytes"],
                                 sum(p.stat().st_size for p in path.rglob("*") if p.is_file()))
                self.assertEqual(account["provider_internal_cached_support_bytes"],
                                 7 * 32 if mode == "ext_cached" else 0)
                self.assertFalse((path / "provider/signing_seed.bin").exists())

    def test_rebuild_reads_all_payloads_every_request_without_retained_tree(self):
        path, descriptor, _ = self.make_case(mode="ext_rebuild")
        before = {p.relative_to(path) for p in path.rglob("*") if p.is_file()}
        for _ in range(2):
            result = query_store(path, 2)
            self.assertEqual(result["provider"]["payload_read_operations"], 3)
            self.assertEqual(result["provider"]["payload_read_bytes"], descriptor["canonical_bytes"])
            self.assertEqual(result["provider"]["support_read_bytes"], 0)
            self.assertEqual(result["provider"]["support_bytes"], 0)
            self.assertTrue(verify_response(descriptor, result["response"]))
        after = {p.relative_to(path) for p in path.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any(p.name in ("leaves.bin", "levels.bin", "support.json") for p in after))

    def test_full_support_reads_only_path_digests(self):
        path, _, _ = self.make_case(n=8)
        stats = query_store(path, 5)["provider"]
        self.assertEqual(stats["support_read_bytes"], 3 * 32)
        self.assertEqual(stats["support_read_operations"], 3)
        self.assertEqual(stats["support_bytes"], 15 * 32)

    def test_wrong_block_root_key_signature_path_and_context_fail_closed(self):
        path, descriptor, _ = self.make_case()
        original = query_store(path, 2)["response"]
        mutations = []
        def altered(edit):
            value = deepcopy(original)
            edit(value)
            mutations.append(value)
        def corrupt_block(value):
            block = bytearray(base64.b64decode(value["block_b64"]))
            block[-1] ^= 1
            value["block_b64"] = base64.b64encode(block).decode()
            
            value["leaf_hex"] = leaf_hash(tuple(value["query"]), bytes(block)).hex()
        altered(corrupt_block)
        altered(lambda r: r["certificate"].update(root_hex="00" * 32))
        altered(lambda r: r["certificate"].update(signature_hex="00" * 64))
        altered(lambda r: r["path"][0].update(digest_hex="00" * 32))
        altered(lambda r: r["path"][0].update(direction=0))  
        altered(lambda r: r["query"].__setitem__(0, 8))
        altered(lambda r: r["query"].__setitem__(1, 12))
        altered(lambda r: r["query"].__setitem__(2, 1))
        altered(lambda r: r["query"].__setitem__(2, 4))  
        altered(lambda r: r.update(n=4))
        altered(lambda r: r.update(n_prime=8))
        altered(lambda r: r.update(mode="ext_cached"))
        altered(lambda r: r["certificate"].update(k=12))
        altered(lambda r: r["certificate"].update(public_key_hex=descriptor["public_key_hex"]))
        altered(lambda r: r.update(public_key_hex=descriptor["public_key_hex"]))
        for i, response in enumerate(mutations):
            with self.subTest(mutation=i):
                self.assertFalse(verify_response(descriptor, response))
        wrong_key = deepcopy(descriptor)
        wrong_key["public_key_hex"] = Ed25519OpenSSL().public_from_seed(b"x" * 32).hex()
        self.assertFalse(verify_response(wrong_key, original))
        other_path, other_descriptor, _ = self.make_case("other", shard=8)
        self.assertFalse(verify_response(other_descriptor, original))
        self.assertFalse(verify_response(descriptor, query_store(other_path, 2)["response"]))

    def test_self_signed_response_cannot_replace_pretrusted_key(self):
        path, descriptor, _ = self.make_case()
        response = query_store(path, 2)["response"]
        backend, attacker_seed = Ed25519OpenSSL(), b"a" * 32
        response["certificate"]["signature_hex"] = backend.sign(attacker_seed, root_statement(
            descriptor["shard"], descriptor["epoch"], descriptor["k"],
            bytes.fromhex(descriptor["root_hex"]))).hex()
        self.assertFalse(verify_response(descriptor, response))
        response["certificate"]["public_key_hex"] = backend.public_from_seed(attacker_seed).hex()
        self.assertFalse(verify_response(descriptor, response))

    def test_leaf_verifier_rejects_changed_leaf_vector_and_padding(self):
        path, descriptor, _ = self.make_case(mode="leaf")
        response = query_store(path, 2)["response"]
        for index in range(4):
            bad = deepcopy(response)
            bad["leaves_hex"][index] = "00" * 32
            self.assertFalse(verify_response(descriptor, bad))
        bad = deepcopy(response)
        bad["leaves_hex"].pop()
        self.assertFalse(verify_response(descriptor, bad))

    def test_malformed_responses_return_false(self):
        path, descriptor, _ = self.make_case()
        original = query_store(path, 2)["response"]
        candidates = [None, [], {}, 1, "bad"]
        for key, value in (("block_b64", "@@@"), ("block_b64", 3),
                           ("block_b64", "A" * ((descriptor["max_block_bytes"] + 2) // 3 * 4 + 4)),
                           ("path", None), ("path", [{}]), ("leaf_hex", "z" * 64),
                           ("query", [7, 11, True]), ("n", True), ("version", True),
                           ("certificate", [])):
            response = deepcopy(original)
            response[key] = value
            candidates.append(response)
        for response in candidates:
            self.assertFalse(verify_response(descriptor, response))
        self.assertFalse(verify_response(None, original))

    def test_setup_refuses_overwrite_and_invalid_inputs(self):
        path, descriptor, _ = self.make_case()
        with self.assertRaises(FileExistsError):
            create_case([b"new"], path, "full")
        self.assertEqual(load_descriptor(path), descriptor)
        for payloads, mode in (([], "full"), ([b"a"], "unknown")):
            with self.assertRaises(ValueError):
                create_case(payloads, self.root / "invalid", mode)
        with self.assertRaises(TypeError):
            create_case(["text"], self.root / "invalid", "full")
        for position in (0, 4, True):
            with self.assertRaises(ValueError):
                query_store(path, position)

    def test_provider_restart_needs_only_persisted_files(self):
        path, _, _ = self.make_case(mode="ext_rebuild")
        script = ("import json,sys; from pathlib import Path; "
                  "from msi_supplement.core import load_descriptor,query_store,verify_response; "
                  "p=Path(sys.argv[1]); print(json.dumps({'valid':verify_response(load_descriptor(p),"
                  "query_store(p,2)['response'])}))")
        output = subprocess.check_output([sys.executable, "-c", script, str(path)], cwd=SUITE, text=True)
        self.assertTrue(json.loads(output)["valid"])


if __name__ == "__main__":
    unittest.main()
