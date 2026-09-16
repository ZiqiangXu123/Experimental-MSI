





from __future__ import annotations

import bisect
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


def _read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip("\0\r\n ")
    except OSError:
        return None


def _proc_numbers(path: str, scale_kib: bool = False) -> dict[str, int]:
    result = {}
    for line in (_read_text(path) or "").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        parts = value.split()
        try:
            number = int(parts[0])
        except (ValueError, IndexError):
            continue
        if scale_kib and len(parts) > 1 and parts[1] == "kB":
            number *= 1024
        result[key] = number
    return result


def _vcgencmd(argument: str) -> str | None:
    executable = shutil.which("vcgencmd")
    if executable is None:
        return None
    try:
        proc = subprocess.run(
            [executable, argument], capture_output=True, text=True,
            timeout=1.0, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _temperature() -> float | None:
    thermal_root = Path("/sys/class/thermal")
    try:
        zones = sorted(thermal_root.glob("thermal_zone*"))
    except OSError:
        zones = []
    preferred = [z for z in zones if any(
        kind in (_read_text(z / "type") or "").lower()
        for kind in ("cpu", "soc", "bcm")
    )]
    for zone in preferred + [z for z in zones if z not in preferred]:
        try:
            value = float(_read_text(zone / "temp") or "nan") / 1000.0
            if math.isfinite(value):
                return value
        except ValueError:
            pass
    reply = _vcgencmd("measure_temp")
    match = re.fullmatch(r"temp=(-?[0-9]+(?:\.[0-9]+)?)'C", reply or "")
    return float(match.group(1)) if match else None


def _mount_unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def _storage_info(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path), "mountpoint": None, "filesystem": None,
        "block_device": None, "device": None,
        "total_bytes": None, "used_bytes": None, "free_bytes": None,
    }
    try:
        usage = shutil.disk_usage(path)
        result.update(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)
    except OSError:
        pass
    matches = []
    for line in (_read_text("/proc/self/mountinfo") or "").splitlines():
        before, separator, after = line.partition(" - ")
        fields, suffix = before.split(), after.split()
        if not separator or len(fields) < 5 or len(suffix) < 2:
            continue
        mountpoint = _mount_unescape(fields[4])
        if str(path) == mountpoint or str(path).startswith(mountpoint.rstrip("/") + "/"):
            matches.append((len(mountpoint), mountpoint, fields[2], suffix[0], suffix[1]))
    if not matches:
        return result
    _, mountpoint, device_id, filesystem, source = max(matches)
    result.update(mountpoint=mountpoint, filesystem=filesystem)
    
    if source.startswith("/dev/"):
        result["block_device"] = _mount_unescape(source)
    device_path = Path("/sys/dev/block") / device_id
    try:
        if not device_path.exists():
            return result
        device_path = device_path.resolve()
        if (device_path / "partition").exists():
            device_path = device_path.parent
        device: dict[str, Any] = {"name": device_path.name}
        for name, relative in (("model", "device/model"), ("vendor", "device/vendor"),
                               ("rotational", "queue/rotational")):
            device[name] = _read_text(device_path / relative)
        sectors = _read_text(device_path / "size")
        device["capacity_bytes"] = int(sectors) * 512 if sectors and sectors.isdigit() else None
        result["device"] = device
    except OSError:
        pass
    return result


def storage_for_path(path: str | Path) -> dict[str, Any]:
    
    return _storage_info(Path(path).resolve())


def _network_interfaces() -> dict[str, Any]:
    
    try:
        entries = sorted(Path("/sys/class/net").iterdir(), key=lambda entry: entry.name)
    except OSError:
        return {"available": False, "interfaces": []}
    interfaces = []
    for entry in entries:
        state = _read_text(entry / "operstate")
        try:
            wireless = (entry / "wireless").is_dir() or (entry / "phy80211").exists()
        except OSError:
            wireless = None
        interfaces.append({"name": entry.name, "operstate": state, "is_wireless": wireless})
    return {"available": True, "interfaces": interfaces}


def system_info() -> dict[str, Any]:
    
    os_info = {}
    for line in (_read_text("/etc/os-release") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"NAME", "ID", "VERSION", "VERSION_ID", "PRETTY_NAME"}:
            os_info[key.lower()] = value.strip('"')
    model = _read_text("/proc/device-tree/model") or _read_text("/sys/firmware/devicetree/base/model")
    hostname, architecture = socket.gethostname(), platform.machine()
    hardware_serial = (_read_text("/proc/device-tree/serial-number")
                       or _read_text("/sys/firmware/devicetree/base/serial-number"))
    machine_id = _read_text("/etc/machine-id")
    if hardware_serial:
        machine_id_source = "device_tree_serial_sha256"
        identity = hardware_serial
    elif machine_id and re.fullmatch(r"[0-9a-fA-F]{32}", machine_id):
        machine_id_source = "linux_machine_id_sha256"
        identity = machine_id
    else:
        machine_id_source = "hostname_architecture_sha256_fallback"
        identity = hostname + "\0" + architecture
    return {
        "hostname": hostname,
        "machine_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "machine_id_source": machine_id_source,
        "pi_model": model,
        "architecture": architecture,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "kernel": platform.release(),
        "platform": platform.system(),
        "cpu_count": os.cpu_count(),
        "mem_total_bytes": _proc_numbers("/proc/meminfo", scale_kib=True).get("MemTotal"),
        "os": os_info,
        "openssl": ssl.OPENSSL_VERSION,
        "storage": storage_for_path(Path.cwd()),
        "network": _network_interfaces(),
    }


def rss_bytes() -> int | None:
    




    statm = _read_text("/proc/self/statm")
    if statm:
        try:
            resident_pages = int(statm.split()[1])
            page_size = os.sysconf("SC_PAGE_SIZE")
            if resident_pages >= 0 and page_size > 0:
                return resident_pages * page_size
        except (IndexError, ValueError, OSError):
            pass
    resident = _proc_numbers("/proc/self/status", scale_kib=True).get("VmRSS")
    return resident if resident is not None and resident >= 0 else None


def snapshot() -> dict[str, Any]:
    




    timestamp = time.time_ns()
    process_cpu_ns = time.process_time_ns()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    resident = rss_bytes()
    proc_io = _proc_numbers("/proc/self/io")
    peak_rss = int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024))
    return {
        "timestamp_unix_ns": timestamp,
        "utc_ns": timestamp,
        "timestamp_utc": datetime.fromtimestamp(timestamp / 1e9, tz=timezone.utc).isoformat(),
        "process_cpu_ns": process_cpu_ns,
        "rss_bytes": resident,
        "peak_rss_bytes": peak_rss,
        "io": proc_io if proc_io else None,
        "minor_faults": usage.ru_minflt,
        "major_faults": usage.ru_majflt,
        "temp_C": _temperature(),
        "throttled": _vcgencmd("get_throttled"),
    }


def summarise(values: Iterable[float]) -> dict[str, Any]:
    
    ordered = sorted(float(v) for v in values)
    if any(not math.isfinite(v) for v in ordered):
        raise ValueError("Summary values must be finite")
    result: dict[str, Any] = {"count": len(ordered)}
    if not ordered:
        result.update({key: None for key in ("median", "p95", "p99", "min", "max", "mean")})
        return result

    def percentile(p: float) -> float:
        position = (len(ordered) - 1) * p
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    result.update(
        median=percentile(0.5), p95=percentile(0.95), p99=percentile(0.99),
        min=ordered[0], max=ordered[-1], mean=statistics.fmean(ordered),
    )
    return result


def atomic_json(path: str | Path, value: Any) -> None:
    
    serialized = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def directory_bytes(path: str | Path) -> dict[str, Any]:
    




    root = Path(path)
    unavailable = {"available": False, "logical_bytes": None, "allocated_bytes": None,
                   "file_count": None}
    if not root.is_dir():
        return {**unavailable, "reason": "directory_missing_or_inaccessible"}
    errors = []
    seen: set[tuple[int, int]] = set()
    logical, allocated, count = 0, 0, 0
    allocation_available = True
    for current, directories, files in os.walk(root, followlinks=False, onerror=errors.append):
        entries = files + [d for d in directories if (Path(current) / d).is_symlink()]
        for name in entries:
            try:
                stat = (Path(current) / name).lstat()
            except OSError as exc:
                errors.append(exc)
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            logical += stat.st_size
            count += 1
            if hasattr(stat, "st_blocks"):
                allocated += stat.st_blocks * 512
            else:
                allocation_available = False
    if errors:
        return {**unavailable, "reason": "incomplete_traversal", "error_count": len(errors)}
    return {"available": True, "logical_bytes": logical,
            "allocated_bytes": allocated if allocation_available else None,
            "allocated_bytes_available": allocation_available, "file_count": count}


def _read_energy_csv(csv_path: str | Path) -> tuple[list[int], list[float], str]:
    timestamps, powers = [], []
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        if "timestamp_unix_s" not in fields:
            raise ValueError("Energy CSV requires timestamp_unix_s")
        if "power_w" in fields:
            mode = "power_w"
        elif {"voltage_v", "current_a"}.issubset(fields):
            mode = "voltage_v_times_current_a"
        else:
            raise ValueError("Energy CSV requires power_w or both voltage_v and current_a")
        for row_number, row in enumerate(reader, start=2):
            try:
                seconds = Decimal(row["timestamp_unix_s"])
                if not seconds.is_finite():
                    raise ValueError("nonfinite timestamp")
                timestamp = int((seconds * 1_000_000_000).to_integral_value())
                if mode == "power_w":
                    power = float(row["power_w"])
                else:
                    voltage, current = float(row["voltage_v"]), float(row["current_a"])
                    if not all(math.isfinite(v) and v >= 0 for v in (voltage, current)):
                        raise ValueError("invalid voltage/current")
                    power = voltage * current
                if not math.isfinite(power) or power < 0:
                    raise ValueError("power must be finite and nonnegative")
            except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                raise ValueError(f"Invalid energy reading at CSV row {row_number}: {exc}") from exc
            if timestamps and timestamp <= timestamps[-1]:
                raise ValueError(f"Energy timestamps must strictly increase (CSV row {row_number})")
            timestamps.append(timestamp)
            powers.append(power)
    if len(timestamps) < 2:
        raise ValueError("Energy CSV needs at least two distinct measured samples")
    return timestamps, powers, mode


def _window_ns(window: dict[str, Any], field: str) -> int:
    value = window.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Window {field} must be integer Unix nanoseconds")
    return value


def _integrate(timestamps: list[int], powers: list[float], start: int, end: int,
               gap_limit_ns: float) -> dict[str, Any]:
    if end <= start:
        raise ValueError("Energy windows must have positive duration")
    if start < timestamps[0] or end > timestamps[-1]:
        raise ValueError("Energy CSV does not cover the full requested window; extrapolation is forbidden")
    first_segment = max(0, bisect.bisect_right(timestamps, start) - 1)
    last_segment = min(len(timestamps) - 2, bisect.bisect_left(timestamps, end) - 1)
    segments = range(first_segment, last_segment + 1)
    gap_ns = max(timestamps[i + 1] - timestamps[i] for i in segments)
    if gap_ns > gap_limit_ns:
        raise ValueError("Energy window crosses a sampling gap greater than 5 times the median interval")
    energies = []
    for i in segments:
        left, right = max(start, timestamps[i]), min(end, timestamps[i + 1])
        interval = timestamps[i + 1] - timestamps[i]
        left_power = powers[i] + (powers[i + 1] - powers[i]) * ((left - timestamps[i]) / interval)
        right_power = powers[i] + (powers[i + 1] - powers[i]) * ((right - timestamps[i]) / interval)
        energies.append((left_power + right_power) * 0.5 * ((right - left) / 1e9))
    energy = math.fsum(energies)
    duration = (end - start) / 1e9
    if not math.isfinite(energy):
        raise ValueError("Integrated energy exceeds finite numeric range")
    return {
        "energy_j": energy, "duration_s": duration, "mean_power_w": energy / duration,
        "measured_samples_inside": bisect.bisect_right(timestamps, end) - bisect.bisect_left(timestamps, start),
        "supporting_samples": last_segment - first_segment + 2,
        "max_sample_interval_s": gap_ns / 1e9,
        "boundary_interpolation": {"start": start not in timestamps, "end": end not in timestamps},
        "coverage_fraction": 1.0,
    }


def analyze_energy(csv_path: str | Path | None, windows: list[dict[str, Any]],
                   output_path: str | Path | None = None) -> dict[str, Any]:
    







    if csv_path is None:
        result = {"available": False, "measurement": "unavailable",
                  "reason": "No external power-meter CSV supplied", "runs": []}
        if output_path is not None:
            atomic_json(output_path, result)
        return result
    timestamps, powers, mode = _read_energy_csv(csv_path)
    intervals = [right - left for left, right in zip(timestamps, timestamps[1:])]
    median_interval_ns = statistics.median(intervals)
    runs, run_ids = [], set()
    for window in windows:
        run_id = window.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in run_ids:
            raise ValueError("Energy window run_id must be a unique nonempty string")
        run_ids.add(run_id)
        start, end = (_window_ns(window, f) for f in ("start_unix_ns", "end_unix_ns"))
        idle_start, idle_end = (_window_ns(window, f) for f in ("idle_start_unix_ns", "idle_end_unix_ns"))
        if max(start, idle_start) < min(end, idle_end):
            raise ValueError("Run and idle baseline windows must not overlap")
        active = _integrate(timestamps, powers, start, end, 5 * median_interval_ns)
        idle = _integrate(timestamps, powers, idle_start, idle_end, 5 * median_interval_ns)
        baseline = idle["mean_power_w"] * active["duration_s"]
        incremental = active["energy_j"] - baseline
        runs.append({
            "run_id": run_id, "start_unix_ns": start, "end_unix_ns": end,
            "idle_start_unix_ns": idle_start, "idle_end_unix_ns": idle_end,
            "total_board_energy_j": active["energy_j"],
            "idle_mean_power_w": idle["mean_power_w"],
            "baseline_energy_for_run_j": baseline,
            "incremental_energy_j": incremental,
            "negative_incremental_energy": incremental < 0,
            "run": active, "idle": idle,
        })
    result = {
        "available": True, "measurement": "external_board_input_power",
        "input_columns": mode, "sample_count": len(timestamps),
        "first_timestamp_unix_ns": timestamps[0], "last_timestamp_unix_ns": timestamps[-1],
        "median_sample_interval_s": median_interval_ns / 1e9,
        "gap_limit_s": 5 * median_interval_ns / 1e9,
        "integration": "trapezoidal_with_linear_boundary_interpolation",
        "runs": runs,
    }
    if output_path is not None:
        atomic_json(output_path, result)
    return result
