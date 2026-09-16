

import json
import hashlib
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from msi_supplement.metrics import analyze_energy, atomic_json, directory_bytes, rss_bytes, summarise, system_info


NANOSECOND = 1_000_000_000


def window(start, end, idle_start=0, idle_end=1, run_id="test"):
    return {"run_id": run_id, "start_unix_ns": int(start * NANOSECOND),
            "end_unix_ns": int(end * NANOSECOND),
            "idle_start_unix_ns": int(idle_start * NANOSECOND),
            "idle_end_unix_ns": int(idle_end * NANOSECOND)}


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "meter.csv"

    def tearDown(self):
        self.temporary.cleanup()

    def readings(self, rows, header="timestamp_unix_s,power_w"):
        self.path.write_text(header + "\n" + "\n".join(
            ",".join(str(value) for value in row) for row in rows
        ) + "\n", encoding="utf-8")

    def test_constant_power_and_measured_baseline(self):
        self.readings([(i, 5) for i in range(5)])
        result = analyze_energy(self.path, [window(2, 4)])
        run = result["runs"][0]
        self.assertEqual(result["measurement"], "external_board_input_power")
        self.assertEqual(run["total_board_energy_j"], 10)
        self.assertEqual(run["idle_mean_power_w"], 5)
        self.assertEqual(run["incremental_energy_j"], 0)
        self.assertFalse(run["negative_incremental_energy"])

    def test_linear_power_boundary_interpolation(self):
        self.readings([(i, 2 + 2 * i) for i in range(5)])
        run = analyze_energy(self.path, [window(1.5, 3.5)])["runs"][0]
        self.assertAlmostEqual(run["total_board_energy_j"], 14.0)
        self.assertAlmostEqual(run["incremental_energy_j"], 8.0)
        self.assertEqual(run["run"]["boundary_interpolation"], {"start": True, "end": True})
        self.assertEqual(run["run"]["supporting_samples"], 4)
        self.assertEqual(run["run"]["measured_samples_inside"], 2)
        self.assertEqual(run["run"]["coverage_fraction"], 1.0)

    def test_voltage_and_current_are_multiplied_per_sample(self):
        self.readings([(0, 5, 1), (1, 5, 1), (2, 5, 2), (3, 5, 4)],
                      header="timestamp_unix_s,voltage_v,current_a")
        run = analyze_energy(self.path, [window(2, 3)])["runs"][0]
        self.assertEqual(run["total_board_energy_j"], 15)
        self.assertEqual(run["incremental_energy_j"], 10)

    def test_negative_incremental_energy_is_preserved(self):
        self.readings([(0, 5), (1, 5), (2, 3), (3, 3)])
        run = analyze_energy(self.path, [window(2, 3)])["runs"][0]
        self.assertEqual(run["incremental_energy_j"], -2)
        self.assertTrue(run["negative_incremental_energy"])

    def test_nanosecond_precision_is_kept_at_modern_unix_epoch(self):
        base = 1_700_000_000 * NANOSECOND
        self.readings([(f"1700000000.00000000{i}", 2) for i in range(1, 5)])
        one_ns = {"run_id": "fine", "start_unix_ns": base + 3, "end_unix_ns": base + 4,
                  "idle_start_unix_ns": base + 1, "idle_end_unix_ns": base + 2}
        run = analyze_energy(self.path, [one_ns])["runs"][0]
        self.assertEqual(run["total_board_energy_j"], 2e-9)

    def test_missing_coverage_is_rejected(self):
        self.readings([(i, 2) for i in range(5)])
        for spec in (window(2, 5), window(2, 3, -1, 1)):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "cover"):
                analyze_energy(self.path, [spec])

    def test_gap_inside_window_is_rejected(self):
        self.readings([(i, 2) for i in (0, 1, 2, 20, 21, 22)])
        with self.assertRaisesRegex(ValueError, "sampling gap"):
            analyze_energy(self.path, [window(2, 20)])

    def test_gap_outside_windows_does_not_imply_missing_window_coverage(self):
        self.readings([(i, 2) for i in (0, 1, 2, 20, 21, 22)])
        run = analyze_energy(self.path, [window(20, 22)])["runs"][0]
        self.assertEqual(run["total_board_energy_j"], 4)

    def test_invalid_readings_are_rejected(self):
        cases = [([(0, 1)], "at least two"),
                 ([(0, 1), (0, 2)], "strictly increase"),
                 ([(1, 1), (0, 2)], "strictly increase"),
                 ([(0, 1), (1, "nan")], "finite"),
                 ([(0, 1), (1, "inf")], "finite"),
                 ([(0, 1), (1, -1)], "nonnegative"),
                 ([(0, 1), ("nan", 2)], "nonfinite timestamp"),
                 ([(0, 1), (1, "")], "Invalid energy")]
        for rows, message in cases:
            with self.subTest(rows=rows):
                self.readings(rows)
                with self.assertRaisesRegex(ValueError, message):
                    analyze_energy(self.path, [window(2, 3)])

    def test_negative_voltage_or_current_is_rejected(self):
        self.readings([(0, -5, -2), (1, 5, 2)],
                      header="timestamp_unix_s,voltage_v,current_a")
        with self.assertRaisesRegex(ValueError, "invalid voltage/current"):
            analyze_energy(self.path, [window(2, 3)])

    def test_invalid_or_overlapping_windows_are_rejected(self):
        self.readings([(i, 2) for i in range(5)])
        cases = [window(2, 2), window(1, 0), window(0.5, 2),
                 {**window(2, 3), "start_unix_ns": 2.0}]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                analyze_energy(self.path, [spec])
        with self.assertRaisesRegex(ValueError, "unique"):
            analyze_energy(self.path, [window(2, 3), window(3, 4)])

    def test_absent_measurement_has_no_energy(self):
        output = Path(self.temporary.name) / "energy.json"
        result = analyze_energy(None, [window(2, 3)], output)
        self.assertFalse(result["available"])
        self.assertEqual(result["measurement"], "unavailable")
        self.assertEqual(result["runs"], [])
        self.assertEqual(json.loads(output.read_text()), result)


class SummaryTests(unittest.TestCase):
    def test_linear_percentiles(self):
        result = summarise(range(1, 101))
        self.assertEqual(result["count"], 100)
        self.assertEqual(result["median"], 50.5)
        self.assertAlmostEqual(result["p95"], 95.05)
        self.assertAlmostEqual(result["p99"], 99.01)
        self.assertEqual(result["mean"], 50.5)
        self.assertEqual((result["min"], result["max"]), (1, 100))

    def test_empty_singleton_and_two_values(self):
        empty = summarise([])
        self.assertEqual(empty["count"], 0)
        self.assertTrue(all(v is None for k, v in empty.items() if k != "count"))
        self.assertEqual(summarise([7])["p99"], 7)
        self.assertAlmostEqual(summarise([0, 10])["p95"], 9.5)

    def test_nonfinite_values_cannot_reach_json(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarise([1, value])


class HostMetricTests(unittest.TestCase):
    def test_hardware_serial_hash_takes_precedence_over_cloned_machine_id(self):
        serial = "10000000abcd1234"
        machine = "0123456789abcdef0123456789abcdef"
        readings = {"/proc/device-tree/serial-number": serial, "/etc/machine-id": machine}
        with patch("msi_supplement.metrics._read_text", side_effect=lambda p: readings.get(str(p))), \
                patch("msi_supplement.metrics._storage_info", return_value={}):
            info = system_info()
        self.assertEqual(info["machine_id"], hashlib.sha256(serial.encode()).hexdigest())
        self.assertEqual(info["machine_id_source"], "device_tree_serial_sha256")
        self.assertNotIn(serial, json.dumps(info))
        self.assertNotIn(machine, json.dumps(info))

    def test_hardware_serial_alternate_device_tree_location(self):
        serial = "10000000ffff1234"
        readings = {"/sys/firmware/devicetree/base/serial-number": serial}
        with patch("msi_supplement.metrics._read_text", side_effect=lambda p: readings.get(str(p))), \
                patch("msi_supplement.metrics._storage_info", return_value={}):
            info = system_info()
        self.assertEqual(info["machine_id"], hashlib.sha256(serial.encode()).hexdigest())
        self.assertEqual(info["machine_id_source"], "device_tree_serial_sha256")

    def test_machine_id_is_hashed_and_raw_value_is_not_returned(self):
        raw = "0123456789abcdef0123456789abcdef"
        with patch("msi_supplement.metrics._read_text", side_effect=lambda p: raw if p == "/etc/machine-id" else None), \
                patch("msi_supplement.metrics._storage_info", return_value={}):
            info = system_info()
        self.assertEqual(info["machine_id"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(info["machine_id_source"], "linux_machine_id_sha256")
        self.assertNotIn(raw, json.dumps(info))

    def test_missing_machine_id_falls_back_to_stable_host_and_architecture_hash(self):
        with patch("msi_supplement.metrics._read_text", return_value=None), \
                patch("msi_supplement.metrics._storage_info", return_value={}), \
                patch("msi_supplement.metrics.socket.gethostname", return_value="test-host"), \
                patch("msi_supplement.metrics.platform.machine", return_value="aarch64"):
            info = system_info()
        self.assertEqual(info["machine_id"], hashlib.sha256(b"test-host\0aarch64").hexdigest())
        self.assertEqual(info["machine_id_source"], "hostname_architecture_sha256_fallback")

    def test_rss_reads_resident_pages_without_starting_a_subprocess(self):
        with patch("msi_supplement.metrics._read_text", return_value="1000 37 5 1 0 20 0"), \
                patch("msi_supplement.metrics.os.sysconf", return_value=4096), \
                patch("msi_supplement.metrics.subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            self.assertEqual(rss_bytes(), 37 * 4096)

    def test_rss_falls_back_to_status_without_substituting_peak(self):
        def reading(path):
            return "VmPeak:\t9000 kB\nVmRSS:\t123 kB\n" if path == "/proc/self/status" else None
        with patch("msi_supplement.metrics._read_text", side_effect=reading):
            self.assertEqual(rss_bytes(), 123 * 1024)
        with patch("msi_supplement.metrics._read_text", return_value=None):
            self.assertIsNone(rss_bytes())


class FileMetricTests(unittest.TestCase):
    def test_directory_size_deduplicates_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "data"
            original.write_bytes(b"12345")
            os.link(original, root / "copy")
            nested = root / "nested"
            nested.mkdir()
            (nested / "more").write_bytes(b"67")
            result = directory_bytes(root)
            self.assertTrue(result["available"])
            self.assertEqual(result["logical_bytes"], 7)
            self.assertEqual(result["file_count"], 2)
            self.assertEqual(result["allocated_bytes"],
                             (original.stat().st_blocks + (nested / "more").stat().st_blocks) * 512)

    def test_directory_symlink_does_not_follow_or_recurse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").write_bytes(b"x")
            (root / "loop").symlink_to(root, target_is_directory=True)
            result = directory_bytes(root)
            self.assertEqual(result["file_count"], 2)
            self.assertEqual(result["logical_bytes"], 1 + (root / "loop").lstat().st_size)

    def test_unavailable_directory_does_not_report_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = directory_bytes(Path(temporary) / "missing")
            self.assertFalse(result["available"])
            self.assertIsNone(result["logical_bytes"])

    def test_atomic_json_preserves_existing_file_if_value_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nested" / "result.json"
            atomic_json(target, {"valid": 1})
            with self.assertRaises(ValueError):
                atomic_json(target, {"invalid": math.nan})
            self.assertEqual(json.loads(target.read_text()), {"valid": 1})
            self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
