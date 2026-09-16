

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from msi_supplement.__main__ import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.manifest = {"suite_version": "test", "role": "hpc", "failed_records": 0,
                         "system_info": {"hostname": "manifest-fixture"}, "config": {}}
        self._write_manifest()
        self.source = {
            "experiment": "energy_workload", "case_id": "fixture-energy-case", "status": "ok",
            "role": "pi-verifier", "platform": {"hostname": "client-fixture", "pi_model": None},
            "data_kind": "test_fixture", "metrics": {"queries": 25, "rss_bytes": None},
            "provenance": {"mode": "ext_cached", "n": 512, "trial": 0,
                           "dataset_sha256": "fixture-dataset", "transport": "local",
                           "cpu_scope": "verifier acceptance CPU only"},
        }
        self.windows = [{"run_id": self.source["case_id"], "start_unix_ns": 2_000_000_000,
                         "end_unix_ns": 12_000_000_000, "idle_start_unix_ns": 0,
                         "idle_end_unix_ns": 1_000_000_000}]
        self._write_windows()
        self._write_rows([self.source])
        self.csv = self.root / "power.csv"
        self.csv.write_text("timestamp_unix_s,power_w\n" + "".join(
            f"{second},{2 if second < 2 else 5}\n" for second in range(13)))

    def tearDown(self):
        self.temporary.cleanup()

    def _write_manifest(self):
        (self.run / "run_manifest.json").write_text(json.dumps(self.manifest))

    def _write_rows(self, rows):
        (self.run / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def _write_windows(self):
        (self.run / "energy_windows.json").write_text(json.dumps(self.windows))

    def _call(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def _energy(self):
        with patch("msi_supplement.report.analyze", return_value=self.run / "report") as analyze:
            result = self._call(["energy", "--run-dir", str(self.run), "--csv", str(self.csv)])
        return result, analyze.call_count

    def _assert_rejected_without_append(self):
        before = (self.run / "results.jsonl").read_bytes()
        (status, _, error), reports = self._energy()
        self.assertEqual(status, 1)
        self.assertTrue(error)
        self.assertEqual(reports, 0)
        self.assertEqual((self.run / "results.jsonl").read_bytes(), before)
        return error

    def test_energy_preserves_workload_identity_and_uses_actual_queries(self):
        (status, _, error), reports = self._energy()
        self.assertEqual((status, error, reports), (0, "", 1))
        original, energy = [json.loads(line) for line in (self.run / "results.jsonl").read_text().splitlines()]
        self.assertEqual(original, self.source)
        self.assertIsNone(original["metrics"]["rss_bytes"])
        self.assertEqual(energy["experiment"], "energy")
        self.assertEqual(energy["source_experiment"], "energy_workload")
        for key in ("case_id", "role", "platform", "data_kind"):
            self.assertEqual(energy[key], self.source[key])
        for key, value in self.source["provenance"].items():
            self.assertEqual(energy["provenance"][key], value)
        measured = energy["metrics"]
        self.assertEqual(measured["queries"], 25)
        self.assertEqual(measured["total_board_energy_j"], 50)
        self.assertEqual(measured["incremental_energy_j"], 30)
        self.assertEqual(measured["total_j_per_query"], 2)
        self.assertAlmostEqual(measured["incremental_j_per_query"], 1.2)
        self.assertEqual(energy["provenance"]["meter_csv_sha256"], hashlib.sha256(self.csv.read_bytes()).hexdigest())
        self.assertTrue(energy["provenance"]["one_trace_measures_one_board"])
        self.assertIn("Whole-board", energy["provenance"]["energy_scope"])
        self.assertTrue((self.run / "energy_analysis.json").is_file())

    def test_identical_meter_and_windows_cannot_be_attached_twice(self):
        self.assertEqual(self._energy()[0][0], 0)
        copied = self.root / "same-trace-different-name.csv"
        copied.write_bytes(self.csv.read_bytes())
        self.csv = copied
        analysis_before = (self.run / "energy_analysis.json").read_bytes()
        error = self._assert_rejected_without_append()
        self.assertIn("already attached", error)
        self.assertEqual((self.run / "energy_analysis.json").read_bytes(), analysis_before)

    def test_missing_failed_or_ambiguous_workload_is_rejected(self):
        cases = [[], [{**self.source, "experiment": "local"}],
                 [{**self.source, "status": "failed"}], [self.source, self.source]]
        for rows in cases:
            with self.subTest(rows=rows):
                self._write_rows(rows)
                error = self._assert_rejected_without_append()
                self.assertIn("exactly one successful energy_workload", error)

    def test_actual_query_count_must_be_a_positive_integer(self):
        for queries in (None, 0, -1, True, 25.0):
            with self.subTest(queries=queries):
                source = copy.deepcopy(self.source)
                source["metrics"]["queries"] = queries
                self._write_rows([source])
                self.assertIn("actual query count", self._assert_rejected_without_append())

    def test_uncovered_meter_window_adds_no_success_record(self):
        self.csv.write_text("timestamp_unix_s,power_w\n0,5\n1,5\n2,5\n")
        self.assertIn("cover", self._assert_rejected_without_append())
        self.assertFalse((self.run / "energy_analysis.json").exists())

    def test_all_windows_require_workload_matches_before_any_append(self):
        self.windows.append({**self.windows[0], "run_id": "missing-second-case"})
        self._write_windows()
        self._assert_rejected_without_append()

    def test_smoke_without_sustained_windows_is_rejected(self):
        self.windows = []
        self._write_windows()
        self.assertIn("paper run", self._assert_rejected_without_append())

    def test_successful_run_returns_zero_and_preserves_options(self):
        with patch("msi_supplement.runner.run_suite", return_value=self.run) as run:
            status, _, error = self._call(["run", "--role", "hpc", "--out", str(self.run),
                                          "--skip-original", "--seed", "19", "--sizes", "1", "16384",
                                          "--trials", "3", "--queries", "400"])
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(run.call_args.args[0]["seed"], 19)
        self.assertTrue(run.call_args.args[0]["skip_original"])
        self.assertEqual(run.call_args.args[0]["sizes"], [1, 16384])
        self.assertEqual(run.call_args.args[0]["trials"], 3)
        self.assertEqual(run.call_args.args[0]["queries"], 400)

    def test_completed_run_with_failed_records_returns_nonzero(self):
        self.manifest["failed_records"] = 2
        self._write_manifest()
        with patch("msi_supplement.runner.run_suite", return_value=self.run):
            status, stdout, stderr = self._call(["run", "--role", "hpc", "--out", str(self.run)])
        self.assertEqual(status, 1)
        self.assertIn(str(self.run), stdout)
        self.assertIn("2 failed record(s)", stderr)

    def test_fatal_manifest_cannot_return_success(self):
        self.manifest["fatal_error"] = {"type": "FixtureError"}
        self._write_manifest()
        with patch("msi_supplement.runner.run_suite", return_value=self.run):
            status, _, _ = self._call(["run", "--role", "hpc", "--out", str(self.run)])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
