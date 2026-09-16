





import csv
import hashlib
import importlib
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest


EXPECTED_COMMENTS = {"R1-1", "R1-2", "R2-1", "R2-2", "R2-3", "R2-4", "R2-8"}
EXPECTED_FLAGS = {
    "real_pi_platform",
    "remote_tcp",
    "real_data",
    "energy",
    "filesystem",
    "memory_pressure",
    "service_disconnect",
    "native_finality",
}
STATUSES = {"measured_here", "partial", "missing", "not_applicable"}
ARTIFACTS = {
    "summary.csv",
    "reviewer_coverage.csv",
    "missing_evidence.json",
    "report.md",
    "report.html",
    "tables.tex",
    "input_hashes.json",
}


class _HTMLInspection(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


class ReportTests(unittest.TestCase):
    

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="msi-report-test-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.analyze = importlib.import_module("msi_supplement.report").analyze

    @staticmethod
    def fixture(case_id="fixture-case", **updates):
        row = {
            "run_id": "fixture-run",
            "case_id": case_id,
            "experiment": "local",
            "platform": "desktop",
            "role": "verifier",
            "data_kind": "test_fixture",
            "benchmark": "encoding",
            "status": "ok",
            "provenance": {"kind": "test_fixture", "data_kind": "test_fixture"},
            "metrics": {"median_ms": 1.25, "p95_ms": 2.5},
        }
        row.update(updates)
        return row

    def write_run(self, rows, name="run", extra_lines=()):
        run = self.root / name
        run.mkdir(parents=True)
        source = run / "results.jsonl"
        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        lines.extend(extra_lines)
        source.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return run, source

    def report_for(self, rows, name="run", extra_lines=()):
        run, source = self.write_run(rows, name, extra_lines)
        report = Path(self.analyze(run))
        self.assertTrue(report.is_dir())
        return report, source

    @staticmethod
    def read_csv(path):
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def json_strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from ReportTests.json_strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from ReportTests.json_strings(item)

    @staticmethod
    def svg_contents(report):
        return sorted(path.read_text(encoding="utf-8") for path in report.rglob("*.svg"))

    def test_required_artifacts_coverage_and_raw_input_hash(self):
        report, source = self.report_for([self.fixture()])
        self.assertFalse(ARTIFACTS - {path.name for path in report.iterdir()})

        coverage = self.read_csv(report / "reviewer_coverage.csv")
        self.assertEqual(len(coverage), 7)
        self.assertEqual({row["comment_id"] for row in coverage}, EXPECTED_COMMENTS)
        self.assertTrue(all(row["status"] in STATUSES for row in coverage))

        flags = json.loads((report / "missing_evidence.json").read_text(encoding="utf-8"))
        self.assertTrue(EXPECTED_FLAGS.issubset(flags))
        for flag in EXPECTED_FLAGS:
            with self.subTest(flag=flag):
                self.assertIsInstance(flags[flag], dict)
                self.assertIn(flags[flag]["status"], STATUSES)
                self.assertIsInstance(flags[flag]["reason"], str)
                self.assertTrue(flags[flag]["reason"].strip())

        hashes = json.loads((report / "input_hashes.json").read_text(encoding="utf-8"))
        self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), set(self.json_strings(hashes)))
        for filename in ("report.md", "report.html"):
            self.assertIn("test_fixture", (report / filename).read_text(encoding="utf-8"))

    def test_summary_preserves_each_row_and_flattens_nested_metric_union(self):
        first = self.fixture(
            "first",
            metrics={"latency": {"median_ms": 1.5, "p95_ms": 3}, "n": 10},
        )
        failed = self.fixture(
            "failed-case", status="failed", error="fixture failure", metrics={"bytes": 71}
        )
        report, _ = self.report_for([first, failed, first])
        rows = self.read_csv(report / "summary.csv")
        self.assertEqual(len(rows), 3, "Every valid raw object, including duplicates and failures, is retained")
        self.assertEqual([row["case_id"] for row in rows].count("first"), 2)
        self.assertEqual([row["case_id"] for row in rows].count("failed-case"), 1)
        expected_metrics = {"metrics.latency.median_ms", "metrics.latency.p95_ms", "metrics.n", "metrics.bytes"}
        self.assertTrue(expected_metrics.issubset(rows[0]))
        for row in rows:
            self.assertEqual(row["run_id"], "fixture-run")
            self.assertEqual(row["benchmark"], "encoding")
            if row["case_id"] == "first":
                self.assertEqual(float(row["metrics.latency.median_ms"]), 1.5)
                self.assertEqual(row["metrics.bytes"], "")
            else:
                self.assertEqual(row["status"], "failed")
                self.assertEqual(float(row["metrics.bytes"]), 71)
                self.assertEqual(row["metrics.latency.median_ms"], "")

    def test_empty_input_still_produces_report_with_warning(self):
        report, _ = self.report_for([])
        self.assertFalse(ARTIFACTS - {path.name for path in report.iterdir()})
        self.assertEqual(self.read_csv(report / "summary.csv"), [])
        for filename in ("report.md", "report.html"):
            content = (report / filename).read_text(encoding="utf-8").lower()
            self.assertIn("warning", content)
            self.assertRegex(content, r"empty|no (?:valid |raw |input |measurement )?(?:result )?(?:rows|records|data|results)")

    def test_malformed_and_non_object_lines_warn_without_losing_valid_rows(self):
        report, _ = self.report_for(
            [self.fixture("surviving-row")],
            extra_lines=["{broken", "null", "[]", '"a string is not a result object"', ""],
        )
        rows = self.read_csv(report / "summary.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["case_id"], "surviving-row")
        for filename in ("report.md", "report.html"):
            content = (report / filename).read_text(encoding="utf-8").lower()
            self.assertIn("warning", content)
            self.assertRegex(content, r"malformed|invalid|non.object|not (?:a |an )?(?:json )?object")

    def test_hostile_labels_are_visible_but_escaped_in_html_and_tex(self):
        label = "<img src=x onerror=alert(1)>&%_#${}~^" + chr(92)
        report, _ = self.report_for([self.fixture(label, benchmark=label)])
        html = (report / "report.html").read_text(encoding="utf-8")
        inspection = _HTMLInspection()
        inspection.feed(html)
        self.assertNotIn(label, html)
        self.assertIn(label, "".join(inspection.text))
        self.assertNotIn(("onerror", "alert(1)"), inspection.attributes)
        self.assertNotIn("<img src=x", html)

        tex = (report / "tables.tex").read_text(encoding="utf-8")
        self.assertNotIn(label, tex)
        for escaped in (r"\&", r"\%", r"\_", r"\#", r"\$", r"\{", r"\}"):
            self.assertIn(escaped, tex)
        self.assertRegex(tex, r"\\(?:textasciitilde\{\}|~\{\})")
        self.assertRegex(tex, r"\\(?:textasciicircum\{\}|\^\{\})")
        self.assertIn(r"\textbackslash{}", tex)

    def test_html_has_no_remote_asset_dependencies(self):
        report, _ = self.report_for([self.fixture()])
        inspection = _HTMLInspection()
        inspection.feed((report / "report.html").read_text(encoding="utf-8"))
        for attribute, value in inspection.attributes:
            if attribute in {"src", "srcset"} and value:
                self.assertNotRegex(value, r"(?i)(?:https?:)?//")
        html = (report / "report.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"(?i)@import\s+(?:url\()?['\"]?https?://")
        self.assertNotRegex(html, r"(?i)url\(['\"]?(?:https?:)?//")

    def test_pi_tags_and_replay_cannot_be_claimed_as_real_or_native_evidence(self):
        row = self.fixture(
            "pi-replay-fixture",
            benchmark="ledger_replay",
            tags=["pi", "raspberry-pi", "real_data", "native_finality"],
            platform="desktop",
            environment={"machine": "x86_64", "platform": "desktop", "is_raspberry_pi": False},
            parameters={"transport": "inprocess", "finality_mode": "replay"},
            metrics={"median_ms": 1.25, "p95_ms": 2.5, "finality_ms": 99},
        )
        report, _ = self.report_for([row])
        flags = json.loads((report / "missing_evidence.json").read_text(encoding="utf-8"))
        for flag in ("real_pi_platform", "remote_tcp", "real_data", "native_finality"):
            with self.subTest(flag=flag):
                self.assertNotEqual(flags[flag]["status"], "measured_here")
        for filename in ("report.md", "report.html"):
            self.assertIn("test_fixture", (report / filename).read_text(encoding="utf-8"))

    def test_exact_duplicate_rows_are_preserved_but_do_not_change_charts(self):
        first = self.fixture("same-case", metrics={"median_ms": 1, "p95_ms": 2})
        distinct = self.fixture("same-case", metrics={"median_ms": 9, "p95_ms": 12})
        baseline, _ = self.report_for([first, distinct], name="baseline")
        duplicate, _ = self.report_for([first, distinct, first], name="with-duplicate")
        original_charts = self.svg_contents(baseline)
        self.assertTrue(original_charts, "Successful numeric fixture measurements should produce SVG charts")
        chart_inspection = _HTMLInspection()
        for svg in original_charts:
            chart_inspection.feed(svg)
        
        
        visible_values = {item.strip() for item in chart_inspection.text}
        self.assertTrue({"1", "2", "9", "12"}.issubset(visible_values))
        self.assertEqual(original_charts, self.svg_contents(duplicate))
        self.assertEqual(len(self.read_csv(duplicate / "summary.csv")), 3)
        self.assertIn("duplicate", (duplicate / "report.md").read_text(encoding="utf-8").lower())

    def test_simulated_measurement_provenance_exercises_positive_evidence_branches(self):
        
        row = self.fixture(
            "simulated-source-record",
            experiment="network",
            platform="raspberry_pi",
            data_kind="ethereum_rpc_finalized_json",
            system_info={"model": "Raspberry Pi 4 Model B", "machine_id": "simulated-pi"},
            provenance={
                "transport": "tcp",
                "client_machine_id": "simulated-client",
                "server_machine_id": "simulated-server",
                "server_host": "192.0.2.5",
                "source": "simulated ethereum RPC finalized JSON input",
                "input_sha256": "1" * 64,
            },
        )
        report, _ = self.report_for([row])
        flags = json.loads((report / "missing_evidence.json").read_text(encoding="utf-8"))
        for flag in ("real_pi_platform", "remote_tcp", "real_data"):
            with self.subTest(flag=flag):
                self.assertEqual(flags[flag]["status"], "measured_here")
        self.assertEqual(flags["native_finality"]["status"], "missing")

        
        
        loopback = dict(row)
        loopback["provenance"] = {**row["provenance"], "server_host": "127.0.0.1"}
        loopback_report, _ = self.report_for([loopback], name="loopback")
        loopback_flags = json.loads((loopback_report / "missing_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(loopback_flags["remote_tcp"]["status"], "missing")

        
        
        labelled_fixture = {**row, "data_kind": "test_fixture"}
        fixture_report, _ = self.report_for([labelled_fixture], name="labelled-fixture")
        fixture_flags = json.loads((fixture_report / "missing_evidence.json").read_text(encoding="utf-8"))
        for flag in ("real_pi_platform", "remote_tcp", "real_data"):
            with self.subTest(fixture_flag=flag):
                self.assertEqual(fixture_flags[flag]["status"], "missing")

    def test_skipped_pi_row_is_not_evidence_and_smoke_profile_is_explicit(self):
        
        run, _ = self.write_run([self.fixture(
            "skipped-simulated-pi", status="skipped", data_kind="synthetic",
            platform={"pi_model": "Raspberry Pi 5 Model B", "architecture": "aarch64"},
            provenance={"transport": "local"},
        )])
        (run / "run_manifest.json").write_text(json.dumps({"profile": "smoke", "version": "test", "config": {"trials": 1}}), encoding="utf-8")
        report = self.analyze(run)
        flags = json.loads((report / "missing_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(flags["real_pi_platform"]["status"], "missing")
        for filename in ("report.md", "report.html"):
            content = (report / filename).read_text(encoding="utf-8")
            self.assertIn("integration smoke", content)
            self.assertIn("not publication evidence", content)


if __name__ == "__main__":
    unittest.main()
