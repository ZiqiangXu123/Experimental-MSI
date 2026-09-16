




from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SUITE = Path(__file__).resolve().parents[1]
if str(SUITE) not in sys.path:
    sys.path.insert(0, str(SUITE))

from msi_supplement import core, service


class LocalServiceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="local-integration-not-pi-", dir=SUITE)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "token.txt"
        service.create_token(self.token_file)
        self.token = service.read_token(self.token_file)
        self.server = service.ExperimentServer(("127.0.0.1", 0), self.root / "provider", self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = service.Client(self.url, self.token, timeout=5)
        self.serial = 0

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def create_remote(self, mode="full"):
        response = self.client.call("/create", {"mode": mode, "payloads_b64": [
            base64.b64encode(json.dumps({"number": i, "record": "opaque"}).encode()).decode()
            for i in range(3)]})
        self.assertEqual(len(response["case_id"]), 24)
        self.assertEqual(response["descriptor"]["mode"], mode)
        self.assertIn("platform", response)
        return response

    def worker_config(self, created, source="network"):
        self.serial += 1
        descriptor = self.root / f"descriptor-{self.serial}.json"
        descriptor.write_text(json.dumps(created["descriptor"]))
        return {"kind": "measure", "descriptor": str(descriptor),
                "provider": self.url, "token_file": str(self.token_file),
                "remote_case_id": created["case_id"], "source": source,
                "case_id": "local-integration-not-pi", "warmup": 1,
                "queries": 3, "positions": [1, 3], "memory_limit_mib": 0}

    def run_worker(self, config, *, succeeds=True):
        self.serial += 1
        config_file = self.root / f"worker-{self.serial}.json"
        result_file = self.root / f"worker-result-{self.serial}.json"
        config_file.write_text(json.dumps(config))
        completed = subprocess.run([sys.executable, "-m", "msi_supplement.worker",
                                    str(config_file), str(result_file)], cwd=SUITE,
                                   capture_output=True, text=True, timeout=30)
        self.assertTrue(result_file.is_file(), completed.stderr)
        result = json.loads(result_file.read_text())
        if succeeds:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("error", result)
        else:
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("error", result)
        return result

    def test_authenticated_health_and_persistent_queries_all_modes(self):
        health = self.client.call("/health", {})
        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["artificial_delay"])
        self.assertEqual(health["wire_format"], "http1-json-base64")
        for mode in core.MODES:
            with self.subTest(mode=mode):
                created = self.create_remote(mode)
                package = self.client.call("/query", {"case_id": created["case_id"], "position": 3})
                self.assertEqual(package["response"]["query"], [1, 1, 3])
                self.assertTrue(core.verify_response(created["descriptor"], package["response"]))
                self.assertGreater(self.client.last_request_bytes, 0)
                self.assertGreater(self.client.last_response_bytes, 0)
                self.assertIn("handler_query_ns", package["provider"])

    def test_authentication_denial_and_token_file_permissions(self):
        denied = service.Client(self.url, "incorrect-token-value", timeout=5)
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                denied.call("/health", {})
        finally:
            denied.close()
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            service.create_token(self.token_file)
        self.assertEqual(service.read_token(self.token_file), self.token)

    def test_disconnect_requires_no_acceptance_and_next_call_recovers(self):
        created = self.create_remote()
        query = {"case_id": created["case_id"], "position": 1}
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.client.call("/query", {**query, "fault": "disconnect"})
        self.assertIsNone(self.client.conn)
        package = self.client.call("/query", query)
        self.assertTrue(core.verify_response(created["descriptor"], package["response"]))

    def test_request_bounds_bad_cases_and_strict_positions(self):
        created = self.create_remote()
        for position in (0, 4, True, 1.5, "1"):
            with self.subTest(position=position), self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                self.client.call("/query", {"case_id": created["case_id"], "position": position})
        for case_id in ("../outside", "f" * 24, 1):
            with self.subTest(case_id=case_id), self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                self.client.call("/query", {"case_id": case_id, "position": 1})
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            self.client.call("/create", {"mode": "full", "payloads_b64": ["@@@"]})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            
            connection.request("POST", "/health", body=b"", headers={
                "Authorization": "Bearer " + self.token,
                "Content-Length": str(service.MAX_BODY + 1)})
            reply = connection.getresponse()
            self.assertEqual(reply.status, 400)
            self.assertIn("error", json.loads(reply.read()))
        finally:
            connection.close()

    def test_network_worker_has_fresh_process_memory_and_separate_timings(self):
        created = self.create_remote("leaf")
        config = self.worker_config(created)
        result = self.run_worker(config)
        self.assertEqual(result["metrics"]["queries"], 3)
        self.assertEqual(result["metrics"]["accepted"], 3)
        self.assertEqual(len(result["provider_samples"]), 3)
        self.assertGreater(result["metrics"]["mean_json_body_bytes"], 0)
        self.assertGreater(result["metrics"]["peak_rss_bytes"], 0)
        
        
        for field in ("baseline_rss_bytes", "rss_bytes"):
            value = result["metrics"][field]
            self.assertTrue(value is None or (type(value) is int and value > 0))
        for sample in result["raw_samples"]:
            self.assertGreaterEqual(sample["elapsed_ns"], sample["accept_ns"])
            self.assertGreaterEqual(sample["accept_cpu_ns"], 0)
        self.assertLess(result["window"]["start_unix_ns"], result["window"]["end_unix_ns"])

    def test_pool_worker_uses_preexisting_proofs_and_reports_pressure(self):
        created = self.create_remote("full")
        pool = self.root / "pool"
        pool.mkdir()
        for position in (1, 3):
            package = self.client.call("/query", {"case_id": created["case_id"], "position": position})
            (pool / f"{position}.json").write_text(json.dumps(package))
        config = self.worker_config(created, source="pool")
        config.update(pool=str(pool), provider=None, memory_limit_mib=256, pressure_mib=2)
        result = self.run_worker(config)
        self.assertEqual(result["metrics"]["accepted"], 3)
        self.assertEqual(result["metrics"]["memory_limit_mib"], 256)
        self.assertEqual(result["metrics"]["pressure_buffer_bytes"], 2 * 1048576)
        self.assertEqual(result["metrics"]["mean_json_body_bytes"], 0)
        self.assertEqual(result["provider_samples"], [])

    def test_worker_rejects_tamper_and_other_valid_outstanding_position(self):
        created = self.create_remote("full")
        config = self.worker_config(created)
        config.update(warmup=0, queries=1, positions=[1])
        genuine_query_store = core.query_store
        def switched(path, position):
            return genuine_query_store(path, 2)
        with patch.object(service.core, "query_store", side_effect=switched):
            result = self.run_worker(config, succeeds=False)
            self.assertIn("does not match outstanding request", result["message"])
        def tampered(path, position):
            package = genuine_query_store(path, position)
            package["response"]["certificate"]["signature_hex"] = "00" * 64
            return package
        with patch.object(service.core, "query_store", side_effect=tampered):
            result = self.run_worker(config, succeeds=False)
            self.assertIn("honest query rejected", result["message"])

    def test_original_e2_worker_runs_original_plan_and_operation_audits(self):
        for scheme in ("B2_full", "B3_leaf"):
            with self.subTest(scheme=scheme):
                result = self.run_worker({"kind": "original", "queries": 2, "warmup": 1,
                                          "n": 3, "seed": 19, "scheme": scheme})
                original = result["original_result"]
                self.assertEqual(original["experiment"], "E2")
                self.assertEqual(original["case"]["scheme"], scheme)
                self.assertTrue(all(original["checks"].values()))
                self.assertEqual(original["operations"]["observed"]["signature_verifications_per_query"], 1)
                self.assertEqual(len(original["timing"]["raw_acceptance_latencies_ns"]), 2)


if __name__ == "__main__":
    unittest.main()
