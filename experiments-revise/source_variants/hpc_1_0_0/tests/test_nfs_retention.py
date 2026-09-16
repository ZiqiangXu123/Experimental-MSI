
from __future__ import annotations

import ctypes.util
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from msi_supplement import migration


@unittest.skipUnless(ctypes.util.find_library("crypto"), "E6 requires system libcrypto")
class RetainedMigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="msi-retention-test-", dir=SUITE_ROOT)
        cls.root = Path(cls.temporary.name)
        
        
        actual_rmtree = migration.shutil.rmtree
        fixture_cleanup_attempts = []

        def busy_fixture_teardown(path, *args, **kwargs):
            
            
            if Path(path).name == "work" or Path(path).name.startswith("e6-migration-"):
                fixture_cleanup_attempts.append(str(path))
                raise OSError(errno.EBUSY, "simulated NFS teardown conflict")
            return actual_rmtree(path, *args, **kwargs)

        with patch.dict(os.environ, {"MSI_KEEP_MIGRATION_WORKSPACES": "1"}), patch.object(
            migration.shutil, "rmtree", side_effect=busy_fixture_teardown
        ):
            cls.records = migration.run_migration_suite(
                cls.root / "results", "smoke", blocks=7, block_bytes=64, repeats=1
            )
            if fixture_cleanup_attempts:
                raise AssertionError(f"Retention attempted fixture teardown: {fixture_cleanup_attempts}")
        cls.run_dir = Path(cls.records[0]["provenance"]["run_directory"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_actual_24_cases_pass_with_retained_truthful_evidence(self) -> None:
        self.assertEqual(len(self.records), 24)
        self.assertEqual(sum(row["metrics"]["fault"] == "none" for row in self.records), 6)
        retained = set()
        for row in self.records:
            with self.subTest(case=row["case_id"]):
                self.assertEqual(row["status"], "ok", row["metrics"].get("error"))
                self.assertTrue(all(row["metrics"]["checks"].values()))
                provenance = row["provenance"]
                self.assertEqual(provenance["workspace_cleanup_policy"], "retain")
                self.assertEqual(provenance["adapter_revision"], migration.ADAPTER_REVISION)
                self.assertFalse(provenance["physical_power_loss_tested"])
                self.assertFalse(provenance["actual_chain_integration_tested"])
                workspaces = provenance["retained_workspaces"]
                self.assertEqual(len(workspaces), 1)
                workspace = Path(workspaces[0])
                self.assertTrue(workspace.is_dir())
                self.assertTrue(workspace.is_relative_to(self.run_dir / "work"))
                retained.add(workspace.name)
                raw = json.loads(Path(provenance["raw_e6_result"]).read_text())
                self.assertEqual(raw["workspace_cleanup_policy"], "retain")
                self.assertEqual(raw["retained_workspaces_after_worker_exit"], workspaces)
                self.assertEqual(raw["residual_workspaces_cleaned_after_worker_exit"], 0)
                self.assertEqual(raw["worker_exit_code"], 0)
                self.assertTrue(raw["all_checks_pass"])
        self.assertEqual(len(retained), 24)
        cleanup = json.loads((self.run_dir / "workspace_cleanup.json").read_text())
        self.assertFalse(cleanup["cleanup_performed"])
        self.assertEqual(cleanup["workspace_cleanup_policy"], "retain")
        self.assertEqual(set(cleanup["remaining_entries"]), retained)
        self.assertEqual(set(cleanup["residual_entries_before_final_cleanup"]), retained)
        plan = json.loads((self.run_dir / "plan.json").read_text())
        self.assertEqual(plan["planned_cases"], 24)
        self.assertEqual(plan["workspace_cleanup_policy"], "retain")
        persisted = [json.loads(line) for line in (self.run_dir / "records.jsonl").read_text().splitlines()]
        self.assertEqual(persisted, self.records)
        source = migration.VENDOR_SOURCE / "msi_refold_policy_exp"
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob("*.py")}
        self.assertEqual(self.records[0]["provenance"]["source_sha256"], hashes)

    def test_retention_preserves_real_source_reclamation_and_recovery_cleanup(self) -> None:
        for row in self.records:
            metrics = row["metrics"]
            workspace = Path(row["provenance"]["retained_workspaces"][0])
            with self.subTest(case=row["case_id"]):
                self.assertEqual(list((workspace / "transactions").glob("*.json")), [])
                self.assertEqual(list((workspace / "msi").glob(".entry.json.*")), [])
                entry = json.loads((workspace / "msi" / "entry.json").read_text())
                self.assertTrue(Path(entry["meta"]["aux_ref"]).is_file())
                if metrics["fault"] == "none":
                    source_mode = metrics["transition"].split("->")[0]
                    suffix = ".json" if source_mode == "ext" else ".bin"
                    self.assertFalse((workspace / "active" / f"source-{source_mode}{suffix}").exists())
                    self.assertGreater(metrics["migration_wall_ns"], 0)
                    self.assertGreater(metrics["io"]["bytes_written"], 0)
                    self.assertGreater(metrics["metadata_bytes_written"], 0)
                    self.assertTrue(metrics["root_preserved"])
                    self.assertTrue(metrics["queries_equivalent"])
                else:
                    self.assertEqual(metrics["child_exit_code"], migration.CRASH_EXIT_CODES[metrics["fault"]])


class RetentionFailureTests(unittest.TestCase):
    def test_invalid_setting_is_rejected_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory(dir=SUITE_ROOT) as temporary:
            output = Path(temporary) / "must-not-exist"
            for value in ("true", "yes", "2", "", " 1"):
                with self.subTest(value=value), patch.dict(os.environ, {"MSI_KEEP_MIGRATION_WORKSPACES": value}):
                    with self.assertRaisesRegex(ValueError, "MSI_KEEP_MIGRATION_WORKSPACES"):
                        migration.run_migration_suite(output, blocks=7, block_bytes=64, repeats=1)
                    self.assertFalse(output.exists())

    @unittest.skipUnless(ctypes.util.find_library("crypto"), "E6 requires system libcrypto")
    def test_nonzero_worker_and_failed_audit_remain_failed_under_retention(self) -> None:
        actual_run = subprocess.run
        with tempfile.TemporaryDirectory(prefix="msi-retention-failure-", dir=SUITE_ROOT) as temporary:
            for injection in ("nonzero_exit", "failed_audit"):
                def run_and_inject(command, **kwargs):
                    completed = actual_run(command, **kwargs)
                    if "--worker" not in command:
                        return completed
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    request = json.loads(Path(command[-1]).read_text())
                    raw_path = Path(request["result_path"])
                    raw = json.loads(raw_path.read_text())
                    self.assertTrue(raw["all_checks_pass"])
                    if injection == "nonzero_exit":
                        return subprocess.CompletedProcess(command, 7, completed.stdout, completed.stderr)
                    raw["checks"]["post_queries_accept"] = False
                    raw["all_checks_pass"] = False
                    raw["error"] = "injected post-query audit failure"
                    raw_path.write_text(json.dumps(raw))
                    return completed

                with self.subTest(injection=injection), patch.dict(os.environ, {"MSI_KEEP_MIGRATION_WORKSPACES": "1"}), patch.object(
                    migration, "TRANSITIONS", ("full->leaf",)
                ), patch.object(migration, "_crash_points", return_value=()), patch.object(
                    migration.subprocess, "run", side_effect=run_and_inject
                ):
                    records = migration.run_migration_suite(
                        Path(temporary) / injection, "smoke", blocks=7, block_bytes=64, repeats=1
                    )
                    self.assertEqual(len(records), 1)
                    row = records[0]
                    self.assertEqual(row["status"], "failed")
                    self.assertEqual(row["metrics"]["successful_clean_repeats"], 0)
                    self.assertIsNone(row["metrics"]["median_ms"])
                    self.assertTrue(Path(row["provenance"]["retained_workspaces"][0]).is_dir())
                    raw = json.loads(Path(row["provenance"]["raw_e6_result"]).read_text())
                    if injection == "nonzero_exit":
                        self.assertEqual(raw["worker_exit_code"], 7)
                        self.assertTrue(raw["all_checks_pass"])
                    else:
                        self.assertEqual(raw["worker_exit_code"], 0)
                        self.assertFalse(row["metrics"]["checks"]["post_queries_accept"])
                        self.assertIn("injected", row["metrics"]["error"])


if __name__ == "__main__":
    unittest.main()
