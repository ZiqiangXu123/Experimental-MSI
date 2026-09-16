






import copy
import csv
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

GROUP_COLUMNS = {
    "experiment", "role", "machine_id", "pi_model", "architecture", "hostname",
    "data_kind", "mode", "scheme", "n", "implementation", "source",
    "network_kind", "memory_limit_mib", "pressure_mib", "dataset_identity",
    "profile", "workload_seed", "queries", "warmup", "n_trials",
    "duplicate_trials_excluded", "warnings",
}
PAIR_COLUMNS = {
    "pi_role", "other_role", "pi_machine_id", "other_machine_id", "pi_model",
    "pi_architecture", "other_architecture", "scheme", "n", "profile",
    "workload_seed", "queries", "warmup", "pi_n_trials", "other_n_trials",
    "pi_median_ms", "other_median_ms", "pi_over_other_ratio",
}


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aggregate-function-test-", dir=SUITE_ROOT
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.aggregate = importlib.import_module("msi_supplement.aggregate").aggregate

    @staticmethod
    def fixture(trial=0, median_ms=2.0):
        
        return {
            "experiment": "local",
            "case_id": f"function-case-{trial}",
            "role": "verifier",
            "platform": {
                "machine_id": "simulated-desktop-a",
                "pi_model": "",
                "architecture": "x86_64",
                "hostname": "simulated-desktop",
            },
            "data_kind": "test_fixture",
            "status": "ok",
            "metrics": {"median_ms": median_ms},
            "provenance": {
                "trial": trial,
                "implementation": "supplement",
                "source": "synthetic-function-test",
                "mode": "resident",
                "n": 64,
                "profile": "paper",
                "workload_seed": 42,
                "queries": 12,
                "warmup": 2,
                "network_kind": "local",
                "memory_limit_mib": 128,
                "pressure_mib": 16,
                "dataset_sha256": "a" * 64,
                "selected_payload_sha256": "b" * 64,
            },
        }

    @classmethod
    def simulated_original(cls, trial, median_ms, pi):
        
        row = cls.fixture(trial, median_ms)
        row.update(experiment="original_e2", data_kind="synthetic")
        row["role"] = "edge-verifier" if pi else "lab-workstation"
        row["platform"] = {
            "machine_id": "simulated-pi" if pi else "simulated-workstation",
            "pi_model": "Raspberry Pi 5 Model B Rev 1.0" if pi else "",
            "architecture": "aarch64" if pi else "x86_64",
            "hostname": "simulated-pi-host" if pi else "simulated-other-host",
        }
        row["provenance"].update(
            implementation="original_e2", source="original_e2", scheme="msi"
        )
        return row

    def write_run(self, rows, name="run", trials=None):
        run = self.root / name
        run.mkdir(parents=True)
        (run / "results.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        if trials is not None:
            (run / "run_manifest.json").write_text(
                json.dumps({"profile": "paper", "config": {"trials": trials}}),
                encoding="utf-8",
            )
        return run

    @staticmethod
    def csv_rows(path):
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return set(reader.fieldnames or ()), list(reader)

    def aggregate_rows(self, rows, name="run", trials=None):
        run = self.write_run(rows, name, trials)
        result = self.aggregate(run)
        self.assertIsInstance(result, dict)
        report = run / "report"
        groups_header, groups = self.csv_rows(report / "grouped_summary.csv")
        pairs_header, pairs = self.csv_rows(report / "paired_platform_comparison.csv")
        self.assertTrue(GROUP_COLUMNS.issubset(groups_header))
        self.assertTrue(PAIR_COLUMNS.issubset(pairs_header))
        warnings = json.loads((report / "aggregation_warnings.json").read_text(encoding="utf-8"))
        return run, groups_header, groups, pairs, warnings

    def test_trial_statistics_flatten_numeric_metrics_without_pooling_queries(self):
        rows = []
        for trial, latency in enumerate((3, 7, 11)):
            row = self.fixture(trial, latency)
            row["metrics"].update({
                "io": {"bytes": 10 + trial * 20, "complete": True},
                "accepted": True,
                "query_samples_ms": [10000 + trial] * (trial + 1),
                "sample_objects": [{"latency_ms": 20000}],
            })
            if trial != 1:
                row["metrics"]["optional_bytes"] = 20 + trial * 10
            rows.append(row)
        _, header, groups, pairs, _ = self.aggregate_rows(rows, trials=3)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(int(group["n_trials"]), 3)
        self.assertEqual(float(group["median_ms_median"]), 7)
        self.assertEqual(float(group["median_ms_min"]), 3)
        self.assertEqual(float(group["median_ms_max"]), 11)
        self.assertEqual(int(group["median_ms_count"]), 3)
        self.assertEqual(float(group["io.bytes_median"]), 30)
        self.assertEqual(int(group["io.bytes_count"]), 3)
        self.assertEqual(float(group["optional_bytes_median"]), 30)
        self.assertEqual(int(group["optional_bytes_count"]), 2)
        self.assertFalse(any(
            token in field
            for field in header
            for token in ("accepted", "complete", "query_samples", "sample_objects")
        ))
        self.assertEqual(pairs, [])

    def test_nonfinite_metrics_are_excluded_and_report_links_are_idempotent(self):
        row = self.fixture()
        row["metrics"].update(nan_value=float("nan"), infinity=float("inf"))
        run = self.write_run([row], trials=1)
        report = run / "report"
        report.mkdir()
        markdown = report / "report.md"
        markdown.write_text("Existing report.\n", encoding="utf-8")
        self.aggregate(run)
        first = markdown.read_text(encoding="utf-8")
        self.aggregate(run)
        self.assertEqual(markdown.read_text(encoding="utf-8"), first)
        self.assertIn("[grouped_summary.csv](grouped_summary.csv)", first)
        header, groups = self.csv_rows(report / "grouped_summary.csv")
        self.assertNotIn("nan_value_median", header)
        self.assertNotIn("infinity_median", header)
        self.assertEqual(int(groups[0]["median_ms_count"]), 1)

    def test_group_identity_keeps_machine_payload_mode_role_and_workload_distinct(self):
        original = self.fixture()
        variants = [original]
        changes = (
            ("platform", "machine_id", "simulated-desktop-b"),
            ("platform", "pi_model", "Raspberry Pi 4 Model B"),
            ("platform", "architecture", "aarch64"),
            ("platform", "hostname", "simulated-second-host"),
            (None, "role", "provider"),
            (None, "data_kind", "synthetic"),
            ("provenance", "selected_payload_sha256", "c" * 64),
            ("provenance", "mode", "offloaded"),
            ("provenance", "n", 128),
            ("provenance", "implementation", "another-adapter"),
            ("provenance", "source", "another-source"),
            ("provenance", "network_kind", "lan"),
            ("provenance", "memory_limit_mib", 256),
            ("provenance", "pressure_mib", 32),
            ("provenance", "profile", "smoke"),
            ("provenance", "workload_seed", 43),
            ("provenance", "queries", 24),
            ("provenance", "warmup", 4),
        )
        for index, (parent, key, value) in enumerate(changes, 1):
            row = copy.deepcopy(original)
            (row if parent is None else row[parent])[key] = value
            row["case_id"] = f"distinct-function-case-{index}"
            variants.append(row)
        _, _, groups, _, _ = self.aggregate_rows(variants)
        self.assertEqual(len(groups), len(variants))
        self.assertTrue(all(int(row["n_trials"]) == 1 for row in groups))
        self.assertGreater(len({row["dataset_identity"] for row in groups}), 1)

    def test_reimported_source_paths_and_case_prefixes_do_not_add_trials(self):
        source = self.root / "source-input"
        source.mkdir()
        first = self.fixture(0, 2)
        first["case_id"] = "source0-function-case-0"
        first["provenance"]["source_run"] = str(source)
        duplicate = copy.deepcopy(first)
        duplicate["case_id"] = "source8-source3-function-case-0"
        duplicate["provenance"]["source_run"] = str(source / ".." / source.name)
        second = self.fixture(1, 10)
        second["provenance"]["source_run"] = str(source)
        _, _, groups, _, warnings = self.aggregate_rows([first, duplicate, second])
        self.assertEqual(len(groups), 1)
        self.assertEqual(int(groups[0]["n_trials"]), 2)
        self.assertEqual(int(groups[0]["duplicate_trials_excluded"]), 1)
        self.assertEqual(float(groups[0]["median_ms_median"]), 6)
        self.assertIn("duplicate", json.dumps(warnings).lower())

    def test_conflicting_same_source_trial_is_excluded_with_a_warning(self):
        source = self.root / "source-input"
        first = self.fixture(0, 2)
        first["provenance"]["source_run"] = str(source)
        conflict = copy.deepcopy(first)
        conflict["case_id"] = "source2-function-case-0"
        conflict["metrics"]["median_ms"] = 900
        clean = self.fixture(1, 6)
        clean["provenance"]["source_run"] = str(source)
        _, _, groups, _, warnings = self.aggregate_rows([first, conflict, clean])
        self.assertEqual(len(groups), 1)
        self.assertEqual(int(groups[0]["n_trials"]), 1)
        self.assertEqual(float(groups[0]["median_ms_median"]), 6)
        self.assertIn("conflict", json.dumps(warnings).lower())

    def test_distinct_source_runs_with_same_trial_number_remain_independent(self):
        first = self.fixture(0, 2)
        first["provenance"]["source_run"] = str(self.root / "source-a")
        second = self.fixture(0, 10)
        second["provenance"]["source_run"] = str(self.root / "source-b")
        _, _, groups, _, _ = self.aggregate_rows([first, second])
        self.assertEqual(len(groups), 1)
        self.assertEqual(int(groups[0]["n_trials"]), 2)
        self.assertEqual(int(groups[0]["duplicate_trials_excluded"]), 0)
        self.assertEqual(float(groups[0]["median_ms_median"]), 6)

    def test_missing_trial_failed_and_unsupported_rows_are_excluded(self):
        valid = self.fixture(0, 2)
        missing_trial = self.fixture(1, 900)
        del missing_trial["provenance"]["trial"]
        failed = self.fixture(1, 10000)
        failed["status"] = "failed"
        skipped = self.fixture(2, 20000)
        skipped["status"] = "skipped"
        unsupported = self.fixture(2, 30000)
        unsupported["experiment"] = "unsupported-function-test"
        _, _, groups, _, warnings = self.aggregate_rows(
            [valid, missing_trial, failed, skipped, unsupported], trials=3
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(int(groups[0]["n_trials"]), 1)
        self.assertEqual(float(groups[0]["median_ms_median"]), 2)
        text = json.dumps(warnings).lower()
        self.assertRegex(text, r"missing[^\n]*trial|trial[^\n]*missing")
        self.assertRegex(text, r"incomplete|insufficient|expected|fewer|only 1")
        self.assertTrue(json.loads(groups[0]["warnings"]))

    def test_measured_metadata_pair_uses_matched_trial_medians_and_custom_roles(self):
        
        rows = []
        for trial, (pi_latency, other_latency) in enumerate(zip((2, 100, 6), (1, 2, 3))):
            for pi, latency in ((True, pi_latency), (False, other_latency)):
                row = self.simulated_original(trial, latency, pi)
                row["metrics"]["query_samples_ms"] = [100000 if pi else 0.001] * (trial + 1)
                rows.append(row)
        rows.append(self.simulated_original(3, 10000, True))
        _, _, _, pairs, _ = self.aggregate_rows(rows)
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["pi_role"], "edge-verifier")
        self.assertEqual(pair["other_role"], "lab-workstation")
        self.assertEqual(pair["pi_machine_id"], "simulated-pi")
        self.assertEqual(pair["other_machine_id"], "simulated-workstation")
        self.assertEqual(pair["pi_architecture"], "aarch64")
        self.assertEqual(pair["other_architecture"], "x86_64")
        self.assertEqual(int(pair["pi_n_trials"]), 3)
        self.assertEqual(int(pair["other_n_trials"]), 3)
        self.assertEqual(float(pair["pi_median_ms"]), 6)
        self.assertEqual(float(pair["other_median_ms"]), 2)
        self.assertEqual(float(pair["pi_over_other_ratio"]), 3)

    def test_pairs_require_genuine_pi_model_and_different_non_pi_architecture(self):
        cases = (
            ("pi-model-absent", "pi", "pi_model", ""),
            ("generic-arm-model", "pi", "pi_model", "ARM development board"),
            ("same-architecture", "other", "architecture", "aarch64"),
            ("other-is-also-pi", "other", "pi_model", "Raspberry Pi 4 Model B"),
        )
        for name, side, key, value in cases:
            with self.subTest(name=name):
                pi = self.simulated_original(0, 6, True)
                other = self.simulated_original(0, 2, False)
                pi["role"] = "raspberry-pi"
                pi["tags"] = ["raspberry_pi", "pi"]
                (pi if side == "pi" else other)["platform"][key] = value
                _, _, _, pairs, _ = self.aggregate_rows([pi, other], name=name)
                self.assertEqual(pairs, [])

    def test_pairs_require_matching_workload_and_original_implementation(self):
        cases = (
            ("scheme", "provenance", "scheme", "baseline"),
            ("size", "provenance", "n", 128),
            ("seed", "provenance", "workload_seed", 43),
            ("profile", "provenance", "profile", "smoke"),
            ("queries", "provenance", "queries", 24),
            ("warmup", "provenance", "warmup", 4),
            ("data-kind", None, "data_kind", "ethereum_rpc_finalized_json"),
            ("implementation", "provenance", "implementation", "supplement"),
            ("experiment", None, "experiment", "local"),
            ("unmatched-trial", "provenance", "trial", 9),
        )
        for name, parent, key, value in cases:
            with self.subTest(name=name):
                pi = self.simulated_original(0, 6, True)
                other = self.simulated_original(0, 2, False)
                (other if parent is None else other[parent])[key] = value
                _, _, _, pairs, _ = self.aggregate_rows([pi, other], name=name)
                self.assertEqual(pairs, [])

        for missing in ("profile", "workload_seed", "queries", "warmup"):
            with self.subTest(missing=missing):
                pi = self.simulated_original(0, 6, True)
                other = self.simulated_original(0, 2, False)
                del pi["provenance"][missing]
                del other["provenance"][missing]
                _, _, _, pairs, _ = self.aggregate_rows([pi, other], name="missing-" + missing)
                self.assertEqual(pairs, [])

    def test_explicit_fixture_rows_never_produce_platform_pairs(self):
        pi = self.simulated_original(0, 6, True)
        other = self.simulated_original(0, 2, False)
        for row in (pi, other):
            row["data_kind"] = "test_fixture"
            row["provenance"]["kind"] = "test_fixture"
        _, _, groups, pairs, _ = self.aggregate_rows([pi, other])
        self.assertEqual(len(groups), 2)
        self.assertEqual(pairs, [])

    def test_seed_alias_pairs_but_different_actual_seeds_do_not(self):
        pi = self.simulated_original(0, 6, True)
        other = self.simulated_original(0, 2, False)
        for row in (pi, other):
            row["provenance"]["seed"] = row["provenance"].pop("workload_seed")
        _, _, _, pairs, _ = self.aggregate_rows([pi, other], name="seed-alias")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(int(pairs[0]["workload_seed"]), 42)
        self.assertEqual(float(pairs[0]["pi_over_other_ratio"]), 3)

        pi = self.simulated_original(0, 6, True)
        other = self.simulated_original(0, 2, False)
        pi["provenance"]["seed"] = 42
        other["provenance"]["seed"] = 43
        _, _, _, pairs, _ = self.aggregate_rows([pi, other], name="actual-seed-mismatch")
        self.assertEqual(pairs, [])


if __name__ == "__main__":
    unittest.main()
