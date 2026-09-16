
from __future__ import annotations

from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from msi_supplement import core, runner, service

SUITE = Path(__file__).resolve().parents[1]
TINY_PROFILE = {"sizes": [1], "queries": 2, "warmup": 1, "trials": 1,
                "pool": 1, "memory_limit_mib": 256, "pressure_mib": 1,
                "idle_seconds": 0}


class TinyRunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-unit-test-not-pi-", dir=SUITE)
        self.root = Path(self.temporary.name)
        self.profile_patch = patch.dict(runner.PROFILES, {"smoke": TINY_PROFILE})
        self.profile_patch.start()

    def tearDown(self):
        self.profile_patch.stop()
        self.temporary.cleanup()

    def run_case(self, name, **options):
        with redirect_stdout(io.StringIO()):
            result = runner.run_suite({"out": self.root / name, "role": "hpc",
                                       "profile": "smoke", "skip_original": True,
                                       "skip_migration": True, "seed": 123,
                                       "network_kind": "unspecified", **options})
        manifest = json.loads((result / "run_manifest.json").read_text())
        rows = [json.loads(line) for line in (result / "results.jsonl").read_text().splitlines()]
        self.assertEqual(manifest["profile"], "smoke")
        self.assertFalse(manifest["publication_ready"])
        self.assertFalse(manifest["native_finality_verified"])
        self.assertNotIn("fatal_error", manifest)
        self.assertEqual(manifest["failed_records"], 0,
                         [row for row in rows if row["status"] == "failed"])
        self.assertEqual(len({row["case_id"] for row in rows}), len(rows))
        self.assertTrue((result / "report/report.html").is_file())
        self.assertIn("integration smoke", (result / "report/report.md").read_text())
        self.assertEqual(json.loads((result / "energy_windows.json").read_text()), [])
        storage_rows = [row for row in rows if row["experiment"] == "storage"]
        self.assertEqual({row["provenance"]["mode"] for row in storage_rows}, set(core.MODES))
        self.assertTrue(all(row["provenance"]["n"] == 1 for row in storage_rows))
        selected_hashes = {row["provenance"]["selected_payload_sha256"] for row in storage_rows}
        self.assertEqual(len(selected_hashes), 1)
        _, payloads = runner.data.load_dataset(result / "datasets/synthetic")
        self.assertEqual(selected_hashes, {hashlib.sha256(b"".join(payloads)).hexdigest()})
        self.assertTrue(all(row["metrics"]["failures"] == 0 for row in rows
                            if row["experiment"] == "integrity"))
        return result, rows

    def test_local_n_one_all_modes_and_fresh_worker_paths(self):
        result, rows = self.run_case("local")
        successful = Counter(row["experiment"] for row in rows if row["status"] == "ok")
        self.assertEqual(successful["storage"], 5)
        self.assertEqual(successful["local"], 5)
        self.assertEqual(successful["memory_pressure"], 4)
        self.assertEqual(successful["archive_retrieval"], 1)
        self.assertEqual(successful["network"], 0)
        self.assertEqual(successful["integrity"], 5)
        descriptors = [core.load_descriptor(case) for case in (result / "cases").iterdir()]
        self.assertEqual(len({item["root_hex"] for item in descriptors}), 1)
        for row in rows:
            if row["experiment"] in {"local", "memory_pressure", "archive_retrieval"}:
                self.assertEqual(row["metrics"]["accepted"], 2)
                self.assertEqual(row["metrics"]["queries"], 2)
                raw = json.loads((result / "raw" / f'{row["case_id"]}.json').read_text())
                self.assertEqual(len(raw["raw_samples"]), 2)

    def test_loopback_n_one_two_part_service_and_runner(self):
        token_file = self.root / "token.txt"
        service.create_token(token_file)
        server = service.ExperimentServer(("127.0.0.1", 0), self.root / "provider-data",
                                          service.read_token(token_file))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            result, rows = self.run_case("loopback", provider=f"http://127.0.0.1:{server.server_address[1]}",
                                         token_file=token_file)
            successful = Counter(row["experiment"] for row in rows if row["status"] == "ok")
            self.assertEqual(successful["network"], 4)
            self.assertEqual(successful["disconnect"], 4)
            self.assertEqual(successful["provider"], 9)
            for row in rows:
                if row["experiment"] == "disconnect":
                    self.assertTrue(row["metrics"]["retrieval_failed_safely"])
                    self.assertTrue(row["metrics"]["recovered"])
                    self.assertEqual(row["metrics"]["false_accepts"], 0)
                if row["experiment"] == "storage" and row["provenance"]["mode"] != "archive":
                    base = row["case_id"].removesuffix("-storage")
                    local_descriptor = result / "cases" / base / "descriptor.json"
                    self.assertEqual(row["metrics"]["descriptor_serialized_bytes"], local_descriptor.stat().st_size)
                    self.assertEqual(row["metrics"]["verifier_total_bytes"], local_descriptor.stat().st_size)
            evidence = json.loads((result / "report/missing_evidence.json").read_text())
            self.assertNotEqual(evidence["remote_tcp"]["status"], "available")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_provider_summary_preserves_unavailable_rss(self):
        self.assertIsNone(runner._provider_summary([{"rss_bytes": None}])["rss_bytes"])
        self.assertEqual(runner._provider_summary([{"rss_bytes": None}, {"rss_bytes": 100}])["rss_bytes"], 100)

    def test_worker_failure_is_persisted_as_failed_record(self):
        with redirect_stdout(io.StringIO()), patch.object(runner, "_child", return_value={"error": "InjectedWorkerFailure", "message": "unit test"}):
            result = runner.run_suite({"out": self.root / "failed", "role": "hpc", "profile": "smoke",
                                       "skip_original": True, "skip_migration": True, "seed": 123})
        manifest = json.loads((result / "run_manifest.json").read_text())
        rows = [json.loads(line) for line in (result / "results.jsonl").read_text().splitlines()]
        failures = [row for row in rows if row["status"] == "failed"]
        self.assertEqual(manifest["failed_records"], 10)
        self.assertEqual(len(failures), 10)
        self.assertTrue(all(row["provenance"]["error"] == "InjectedWorkerFailure" for row in failures))


if __name__ == "__main__":
    unittest.main()
