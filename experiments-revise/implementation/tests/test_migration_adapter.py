
from __future__ import annotations

import ctypes.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from msi_supplement.migration import CRASH_EXIT_CODES, TRANSITIONS, run_migration_suite


@unittest.skipUnless(ctypes.util.find_library("crypto"), "Original E6 requires system libcrypto with Ed25519")
class MigrationAdapterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="msi-migration-test-", dir=SUITE_ROOT)
        cls.root = Path(cls.temporary.name)
        cls.outside = cls.root / "inherited-outside-location"
        cls.outside.mkdir()
        cls.sentinel = cls.outside / "do-not-touch.txt"
        cls.sentinel.write_text("user data", encoding="utf-8")
        with patch.dict(os.environ, {
            "E6_NODE_LOCAL_BASE": str(cls.outside),
            "E6_SHARED_WORK_BASE": str(cls.outside),
            "E6_KEEP_WORKSPACES": "1",
        }):
            cls.records = run_migration_suite(cls.root / "results", "smoke", blocks=7, block_bytes=64, repeats=1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_six_directions_preserve_root_anchor_metadata_and_valid_queries(self) -> None:
        clean = [row for row in self.records if row["metrics"]["fault"] == "none"]
        self.assertEqual(len(clean), 6)
        self.assertEqual({row["metrics"]["transition"] for row in clean}, set(TRANSITIONS))
        for row in clean:
            with self.subTest(case=row["case_id"]):
                self.assertEqual(row["status"], "ok", row["metrics"].get("error"))
                metrics = row["metrics"]
                self.assertTrue(all(metrics["checks"].values()), metrics["checks"])
                self.assertEqual(metrics["root_hex_before"], metrics["root_hex_after"])
                self.assertEqual(metrics["protected_sha256_before"], metrics["protected_sha256_after"])
                self.assertEqual(set(metrics["metadata_differences"]), {"meta.mode", "meta.aux_ref"})
                self.assertEqual(metrics["pre_query_audit"]["positions_checked"], 7)
                self.assertEqual(metrics["post_query_audit"]["positions_checked"], 7)
                self.assertTrue(metrics["root_preserved"])
                self.assertTrue(metrics["queries_equivalent"])
                self.assertGreater(metrics["migration_wall_ns"], 0)
                self.assertGreater(metrics["io"]["bytes_written"], 0)
                self.assertGreater(metrics["median_ms"], 0)

    def test_real_crashes_recover_old_or_new_state_at_commit_boundary(self) -> None:
        crashes = [row for row in self.records if row["metrics"]["fault"] != "none"]
        self.assertEqual(len(crashes), 18)
        self.assertEqual({row["metrics"]["fault"] for row in crashes}, {
            "crash_before_metadata_swap", "crash_during_metadata_write", "crash_after_metadata_swap",
        })
        for row in crashes:
            metrics = row["metrics"]
            with self.subTest(case=row["case_id"]):
                self.assertEqual(row["status"], "ok", metrics.get("error"))
                self.assertEqual(metrics["child_exit_code"], CRASH_EXIT_CODES[metrics["fault"]])
                source, target = metrics["transition"].split("->")
                expected = target if metrics["fault"] == "crash_after_metadata_swap" else source
                self.assertEqual(metrics["active_mode"], expected)
                self.assertEqual(metrics["recovery"]["active_mode"], expected)
                self.assertTrue(metrics["queries_equivalent"])
                self.assertEqual(metrics["crash_failures"], 0)
                if metrics["fault"] == "crash_during_metadata_write":
                    self.assertEqual(metrics["recovery"]["temporary_metadata_files"], 1)
                self.assertFalse(row["provenance"]["physical_power_loss_tested"])
                self.assertFalse(row["provenance"]["actual_chain_integration_tested"])

    def test_outputs_are_actual_persisted_evidence_and_cleanup_stays_local(self) -> None:
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "user data")
        self.assertEqual(list(self.outside.iterdir()), [self.sentinel])
        run_dirs = {Path(row["provenance"]["run_directory"]) for row in self.records}
        self.assertEqual(len(run_dirs), 1)
        run_dir = run_dirs.pop()
        self.assertTrue(run_dir.is_relative_to(self.root / "results"))
        self.assertEqual(list((run_dir / "work").iterdir()), [])
        stored = [json.loads(line) for line in (run_dir / "records.jsonl").read_text().splitlines()]
        self.assertEqual(stored, self.records)
        for row in self.records:
            self.assertEqual(row["experiment"], "migration")
            self.assertEqual(row["role"], "verifier")
            self.assertEqual(row["data_kind"], "synthetic")
            self.assertIsNotNone(row["platform"]["filesystem"]["block_size"])
            raw_path = Path(row["provenance"]["raw_e6_result"])
            raw = json.loads(raw_path.read_text())
            self.assertTrue(raw["all_checks_pass"])
            self.assertIn(row["case_id"], Path(row["provenance"]["worker_log"]).read_text())


class MigrationBoundsTests(unittest.TestCase):
    def test_invalid_sizes_and_profiles_are_rejected_before_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=SUITE_ROOT) as temporary:
            output = Path(temporary) / "must-not-be-created"
            for kwargs in ({"blocks": 0}, {"blocks": 65537}, {"block_bytes": 31},
                           {"repeats": 0}, {"repeats": 31}, {"blocks": True},
                           {"blocks": 65536, "block_bytes": 1048576}, {"profile": "unknown"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    run_migration_suite(output, **kwargs)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
