






from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import multiprocessing
import os
import platform as platform_module
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

SUITE_ROOT = Path(__file__).resolve().parents[1]
VENDOR_SOURCE = (
    SUITE_ROOT / "vendor" / "experiment" / "experiments"
    / "e6_refolding_policy" / "src"
)
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_BLOCKS = 65536
MAX_REPEATS = 30
ADAPTER_REVISION = "nfs-retain-1"
TRANSITIONS = ("full->leaf", "leaf->full", "full->ext", "leaf->ext", "ext->leaf", "ext->full")
CRASH_EXIT_CODES = {
    "crash_after_root_verify": 81,
    "crash_after_target_create": 82,
    "crash_before_metadata_swap": 83,
    "crash_during_metadata_write": 86,
    "crash_after_metadata_swap": 87,
}


def _vendor() -> tuple[Any, Any, Any]:
    if not (VENDOR_SOURCE / "msi_refold_policy_exp" / "migration.py").is_file():
        raise RuntimeError(f"Original E6 implementation is missing: {VENDOR_SOURCE}")
    source = str(VENDOR_SOURCE)
    if source not in sys.path:
        sys.path.insert(0, source)
    modules = tuple(importlib.import_module(f"msi_refold_policy_exp.{name}")
                    for name in ("dataset", "migration", "state"))
    for module in modules:
        if not Path(module.__file__).resolve().is_relative_to(VENDOR_SOURCE.resolve()):
            raise RuntimeError("An unrelated msi_refold_policy_exp package was already imported")
    return modules


def _system_info() -> dict[str, Any]:
    try:
        from .metrics import system_info
    except ImportError:
        return {"system": platform_module.system(), "release": platform_module.release(),
                "machine": platform_module.machine(), "python": sys.version,
                "cpu_count": os.cpu_count(), "collector": "stdlib"}
    return system_info()


def _snapshot() -> dict[str, Any]:
    try:
        from .metrics import snapshot
    except ImportError:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {"timestamp_unix_ns": time.time_ns(), "process_cpu_ns": time.process_time_ns(),
                "peak_rss_kib": int(usage.ru_maxrss) if sys.platform != "darwin" else int(usage.ru_maxrss / 1024),
                "collector": "stdlib"}
    return snapshot()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _crash_points(profile: str) -> tuple[str, ...]:
    if profile == "smoke":
        return ("crash_before_metadata_swap", "crash_during_metadata_write", "crash_after_metadata_swap")
    if profile in {"paper", "full"}:
        return tuple(CRASH_EXIT_CODES)
    raise ValueError("profile must be smoke or paper (full is an alias for paper)")


def _execute_case(case: dict[str, Any], dataset_dir: Path, work_root: Path,
                  keep_workspace: bool = False) -> dict[str, Any]:
    
    dataset, original, state = _vendor()
    
    
    os.environ["E6_SHARED_WORK_BASE"] = str(work_root.resolve())
    os.environ["E6_NODE_LOCAL_BASE"] = str(work_root.resolve())
    os.environ["E6_KEEP_WORKSPACES"] = "0"
    dataset_root = dataset.dataset_path(dataset_dir, case["dataset_id"])
    workspace, before, manifest = original._prepare_workspace(case, dataset_root, work_root)
    if not workspace.resolve().is_relative_to(work_root.resolve()):
        raise RuntimeError("Original E6 workspace escaped the supplied work root")
    telemetry_before = _snapshot()
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    try:
        entry_path = workspace / "msi" / "entry.json"
        pre = original.audit_entry_queries(entry_path, seed=case["seed"], exact_sample_limit=32)
        target_mode = case["target_mode"]
        fault = case["fault"]
        raw: dict[str, Any] = {"case": case, "filesystem": original._filesystem_info(workspace),
                               "pre_query_audit": pre, "workspace_path": str(workspace),
                               "workspace_cleanup_policy": "retain" if keep_workspace else "remove"}
        if fault == "none":
            raw["migration"] = original.migrate_epoch(
                workspace=workspace, dataset_root=dataset_root, manifest=manifest,
                target_mode=target_mode,
            )
        else:
            
            
            child = multiprocessing.get_context("spawn").Process(
                target=original._child_migration,
                args=(str(workspace), str(dataset_root), manifest, target_mode, "none", fault),
            )
            crash_start = time.perf_counter_ns()
            child.start()
            child.join(timeout=case["crash_timeout_seconds"])
            if child.is_alive():
                child.kill()
                child.join()
                raise RuntimeError("E6 crash-injection child exceeded timeout")
            raw["crash_process_wall_ns"] = time.perf_counter_ns() - crash_start
            raw["child_exit_code"] = child.exitcode
            if child.exitcode != CRASH_EXIT_CODES[fault]:
                raise RuntimeError(f"Expected injection exit {CRASH_EXIT_CODES[fault]}, got {child.exitcode}")
            raw["recovery"] = original.recover_after_failure(
                workspace, before=before, expected_target_mode=target_mode, fault=fault,
            )
        after = state.read_entry(entry_path)
        raw["post_query_audit"] = original.audit_entry_queries(
            entry_path, seed=case["seed"], exact_sample_limit=32,
        )
        validation = state.validate_active_state(entry_path)
        protected_before = state.protected_bytes(before)
        protected_after = state.protected_bytes(after)
        allowed, differences = state.allowed_difference_only(before, after)
        expected_mode = (target_mode if fault in {"none", "crash_after_metadata_swap"}
                         else case["source_mode"])
        checks = {
            "root_equal": before["root_hex"] == after["root_hex"],
            "protected_bytes_equal": protected_before == protected_after,
            "anchor_bytes_equal": state.canonical_json_bytes(before["anchor"]) == state.canonical_json_bytes(after["anchor"]),
            "allowed_metadata_difference_only": allowed or before == after,
            "active_mode_correct": after["meta"]["mode"] == expected_mode,
            "active_state_valid": validation["status"] == "PASS",
            "pre_queries_accept": bool(pre["all_accept"]),
            "post_queries_accept": bool(raw["post_query_audit"]["all_accept"]),
            "dataset_matches_case": int(manifest["spec"]["n"]) == case["n"],
            "no_pending_journals": not any((workspace / "transactions").glob("*.json")),
            "no_partial_metadata": not any(entry_path.parent.glob(".entry.json.*")),
        }
        if fault != "none":
            checks["injected_exit_code_matches"] = raw["child_exit_code"] == CRASH_EXIT_CODES[fault]
            checks["recovery_checks_pass"] = all(
                bool(raw["recovery"][name]) for name in
                ("active_mode_correct", "active_state_valid", "protected_bytes_equal", "allowed_metadata_difference_only")
            )
        raw.update({
            "root_hex_before": before["root_hex"], "root_hex_after": after["root_hex"],
            "protected_sha256_before": hashlib.sha256(protected_before).hexdigest(),
            "protected_sha256_after": hashlib.sha256(protected_after).hexdigest(),
            "metadata_differences": differences, "active_mode": after["meta"]["mode"],
            "expected_active_mode": expected_mode, "checks": checks,
            "all_checks_pass": all(checks.values()),
            "case_wall_ns": time.perf_counter_ns() - started,
            "case_cpu_ns": time.process_time_ns() - cpu_started,
        })
        usage_after = resource.getrusage(resource.RUSAGE_SELF)
        raw["worker_resources"] = {
            "peak_rss_kib": int(usage_after.ru_maxrss) if sys.platform != "darwin" else int(usage_after.ru_maxrss / 1024),
            "minor_faults": int(usage_after.ru_minflt - usage_before.ru_minflt),
            "major_faults": int(usage_after.ru_majflt - usage_before.ru_majflt),
            "scope": "case worker; spawned crashing child excluded",
        }
        raw["snapshot_before"] = telemetry_before
        raw["snapshot_after"] = _snapshot()
        return raw
    finally:
        
        
        if not keep_workspace:
            shutil.rmtree(workspace)


def run_migration_suite(output_dir: Path, profile: str = "smoke", blocks: int = 512,
                        block_bytes: int = 2048, repeats: int = 3) -> list[dict[str, Any]]:
    













    crash_points = _crash_points(profile)
    for name, value in (("blocks", blocks), ("block_bytes", block_bytes), ("repeats", repeats)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if not 1 <= blocks <= MAX_BLOCKS:
        raise ValueError(f"blocks must be between 1 and {MAX_BLOCKS}")
    if not 32 <= block_bytes <= 1024 * 1024:
        raise ValueError("block_bytes must be between 32 and 1048576")
    if not 1 <= repeats <= MAX_REPEATS:
        raise ValueError(f"repeats must be between 1 and {MAX_REPEATS}")
    if blocks * block_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError("Generated payload exceeds the 128 MiB limit")
    keep_setting = os.environ.get("MSI_KEEP_MIGRATION_WORKSPACES", "0")
    if keep_setting not in {"0", "1"}:
        raise ValueError("MSI_KEEP_MIGRATION_WORKSPACES must be 0 or 1")
    keep_workspaces = keep_setting == "1"
    cleanup_policy = "retain" if keep_workspaces else "remove"
    dataset, original, _state = _vendor()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="migration-", dir=output_dir))
    dataset_dir, work_root = run_dir / "datasets", run_dir / "work"
    raw_dir, log_dir = run_dir / "raw-e6", run_dir / "logs"
    for directory in (dataset_dir, work_root, raw_dir, log_dir):
        directory.mkdir()
    
    estimated_disk_bound = blocks * block_bytes + (1 << (blocks - 1).bit_length()) * 512 + 8 * 1024 * 1024
    planned_cases = len(TRANSITIONS) * (repeats + len(crash_points))
    if keep_workspaces:
        
        
        
        estimated_disk_bound += planned_cases * ((1 << (blocks - 1).bit_length()) * 256 + 1024 * 1024)
    if shutil.disk_usage(run_dir).free < estimated_disk_bound:
        raise OSError(f"Insufficient free space for bounded E6 run ({estimated_disk_bound} bytes)")
    spec = dataset.make_dataset_spec(
        shard=3, epoch=17, n=blocks, block_bytes=block_bytes,
        layout="epoch_packed", codec="raw-v1", payload_seed=20260915, anchor_key_seed=1703,
    )
    dataset_check = dataset.build_dataset(dataset_dir, spec)
    if dataset_check["status"] != "PASS":
        raise RuntimeError(f"E6 dataset validation failed: {dataset_check}")
    machine = _system_info()
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((VENDOR_SOURCE / "msi_refold_policy_exp").glob("*.py"))}
    cases = []
    for transition in TRANSITIONS:
        source_mode, target_mode = transition.split("->")
        for repeat, fault in [(r, "none") for r in range(repeats)] + [(0, point) for point in crash_points]:
            case_id = f"migration-{source_mode}-to-{target_mode}-{fault}-r{repeat}"
            cases.append({
                "case_id": case_id, "dataset_id": spec.dataset_id,
                "transition": transition, "source_mode": source_mode, "target_mode": target_mode,
                "backend": "shared_scratch", "trial_kind": "equivalence_audit" if fault == "none" else "fault_injection",
                "n": blocks, "n_prime": spec.n_prime, "block_bytes": block_bytes,
                "seed": 11, "repeat": repeat, "fault": fault, "crash_timeout_seconds": 120,
            })
    _write_json(run_dir / "plan.json", {
        "profile": profile, "cases": cases, "dataset_validation": dataset_check,
        "estimated_disk_bound_bytes": estimated_disk_bound,
        "adapter_revision": ADAPTER_REVISION,
        "workspace_cleanup_policy": cleanup_policy,
        "planned_cases": planned_cases,
        "crash_model": "process termination by os._exit at named original E6 injection points",
        "ext_backend": "local filesystem registry and witness support file",
    })
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(SUITE_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["E6_SHARED_WORK_BASE"] = str(work_root)
    env["E6_NODE_LOCAL_BASE"] = str(work_root)
    env["E6_KEEP_WORKSPACES"] = "0"
    records = []
    records_path = run_dir / "records.jsonl"
    for case in cases:
        raw_path = raw_dir / f"{case['case_id']}.json"
        request_path = raw_dir / f"{case['case_id']}.input.json"
        log_path = log_dir / f"{case['case_id']}.txt"
        _write_json(request_path, {"case": case, "dataset_dir": str(dataset_dir),
                                   "work_root": str(work_root), "result_path": str(raw_path),
                                   "keep_workspace": keep_workspaces})
        command = [sys.executable, "-m", "msi_supplement.migration", "--worker", str(request_path)]
        started = time.perf_counter_ns()
        try:
            completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
            log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
            raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {
                "all_checks_pass": False, "error": "E6 worker produced no result",
            }
            raw["worker_exit_code"] = completed.returncode
        except subprocess.TimeoutExpired as exc:
            def text_output(value: Any) -> str:
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            log_path.write_text(text_output(exc.stdout) + text_output(exc.stderr) + "\nWorker timed out\n", encoding="utf-8")
            raw = {"all_checks_pass": False, "error": "E6 worker timed out", "worker_exit_code": None}
        raw["worker_including_startup_wall_ns"] = time.perf_counter_ns() - started
        
        
        
        residual = list(work_root.glob(f"e6-{case['case_id']}-*"))
        if not keep_workspaces:
            for candidate in residual:
                if candidate.is_symlink():
                    candidate.unlink()
                elif candidate.is_dir():
                    shutil.rmtree(candidate)
        raw["residual_workspaces_cleaned_after_worker_exit"] = 0 if keep_workspaces else len(residual)
        raw["retained_workspaces_after_worker_exit"] = [str(p) for p in residual] if keep_workspaces else []
        raw["workspace_cleanup_policy"] = cleanup_policy
        _write_json(raw_path, raw)
        checks = raw.get("checks", {})
        measurement = raw.get("migration", {})
        metrics = {
            "transition": case["transition"], "trial_kind": case["trial_kind"],
            "repeat": case["repeat"], "fault": case["fault"], "blocks": blocks,
            "padded_blocks": spec.n_prime, "block_bytes": block_bytes,
            "logical_payload_bytes": blocks * block_bytes,
            "migration_wall_ns": measurement.get("wall_ns"),
            "migration_cpu_ns": measurement.get("cpu_ns"),
            "case_wall_ns": raw.get("case_wall_ns"),
            "worker_including_startup_wall_ns": raw["worker_including_startup_wall_ns"],
            "hash_calls": measurement.get("hash_calls"), "io": measurement.get("io"),
            "temporary_disk_bytes": measurement.get("temporary_disk_bytes"),
            "target_serialized_bytes": measurement.get("target_serialized_bytes"),
            "metadata_bytes_written": measurement.get("metadata_bytes_written"),
            "write_amplification": measurement.get("write_amplification"),
            "worker_resources": raw.get("worker_resources"),
            "snapshot_before": raw.get("snapshot_before"), "snapshot_after": raw.get("snapshot_after"),
            "checks": checks, "pre_query_audit": raw.get("pre_query_audit"),
            "post_query_audit": raw.get("post_query_audit"), "recovery": raw.get("recovery"),
            "child_exit_code": raw.get("child_exit_code"),
            "root_hex_before": raw.get("root_hex_before"), "root_hex_after": raw.get("root_hex_after"),
            "protected_sha256_before": raw.get("protected_sha256_before"),
            "protected_sha256_after": raw.get("protected_sha256_after"),
            "metadata_differences": raw.get("metadata_differences"),
            "active_mode": raw.get("active_mode"), "expected_active_mode": raw.get("expected_active_mode"),
            "root_preserved": bool(checks.get("root_equal")),
            "queries_equivalent": bool(checks.get("pre_queries_accept") and checks.get("post_queries_accept")),
            "crash_cases": int(case["fault"] != "none"),
            "crash_failures": int(case["fault"] != "none" and not raw.get("all_checks_pass")),
        }
        if raw.get("error"):
            metrics["error"] = raw["error"]
        record = {
            "experiment": "migration", "case_id": case["case_id"], "role": "verifier",
            "platform": {**machine, "filesystem": raw.get("filesystem", original._filesystem_info(run_dir))},
            "data_kind": "synthetic",
            "status": "ok" if raw.get("all_checks_pass") and raw["worker_exit_code"] == 0 else "failed",
            "metrics": metrics,
            "provenance": {
                "implementation": "uploaded E6 msi_refold_policy_exp local-filesystem migration",
                "adapter_revision": ADAPTER_REVISION,
                "workspace_cleanup_policy": cleanup_policy,
                "retained_workspaces": raw["retained_workspaces_after_worker_exit"],
                "retention_scope": "test fixture teardown only; original source reclamation, journals, fsync and recovery remain enabled",
                "source_relative_path": str(VENDOR_SOURCE.relative_to(SUITE_ROOT)),
                "source_sha256": source_hashes, "dataset_id": spec.dataset_id,
                "dataset_seed": spec.payload_seed, "profile": profile,
                "raw_e6_result": str(raw_path), "worker_log": str(log_path), "run_directory": str(run_dir),
                "ext_backend": "local filesystem registry and witness support file; no remote I/O",
                "crash_model": "process os._exit at original E6 injection points",
                "physical_power_loss_tested": False, "actual_chain_integration_tested": False,
                "cache_policy": "OS-managed; no cache flush or privileged cache operation",
                "measurement_scope": "migration time excludes pre/post audits; case time includes audits and crash recovery; worker time also includes interpreter startup",
                "io_scope": "original E6 logical byte counters, not block-device I/O; no cold-cache claim",
                "query_equivalence_scope": "valid dataset queries at recorded audit positions; this is not a universal equivalence proof",
                "snapshot_scope": "case worker excluding crashing child; telemetry collected outside timed case interval",
            },
        }
        records.append(record)
        with records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    
    
    for transition in TRANSITIONS:
        selected = [row for row in records if row["metrics"]["transition"] == transition]
        samples = sorted(row["metrics"]["migration_wall_ns"] / 1e6 for row in selected
                         if row["status"] == "ok" and row["metrics"]["fault"] == "none")
        if samples:
            position = (len(samples) - 1) * 0.95
            lower = int(position)
            upper = min(lower + 1, len(samples) - 1)
            p95 = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
        else:
            p95 = None
        for record in selected:
            record["metrics"].update({"median_ms": statistics.median(samples) if samples else None,
                                       "p95_ms": p95, "successful_clean_repeats": len(samples)})
            record["provenance"]["summary_scope"] = "median/p95 summarize successful clean repeats of this direction; p95 uses linear interpolation"
    records_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8")
    
    
    
    
    cleanup_started = time.perf_counter_ns()
    final_residual = sorted(path.name for path in work_root.iterdir())
    if not keep_workspaces:
        shutil.rmtree(work_root)
        work_root.mkdir()
    original._directory_fsync(work_root)
    original._directory_fsync(run_dir)
    _write_json(run_dir / "workspace_cleanup.json", {
        "phase": "after all case subprocesses exited",
        "adapter_revision": ADAPTER_REVISION,
        "workspace_cleanup_policy": cleanup_policy,
        "cleanup_performed": not keep_workspaces,
        "residual_entries_before_final_cleanup": final_residual,
        "remaining_entries": sorted(path.name for path in work_root.iterdir()),
        "cleanup_wall_ns": time.perf_counter_ns() - cleanup_started,
        "scope": "fresh run-private work directory only; excluded from migration timing",
    })
    return records


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", default="smoke")
    parser.add_argument("--blocks", type=int, default=512)
    parser.add_argument("--block-bytes", type=int, default=2048)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        request = json.loads(args.worker.read_text(encoding="utf-8"))
        try:
            result = _execute_case(request["case"], Path(request["dataset_dir"]), Path(request["work_root"]),
                                   keep_workspace=bool(request.get("keep_workspace", False)))
        except Exception as exc:
            result = {"all_checks_pass": False, "error": f"{type(exc).__name__}: {exc}"}
            traceback.print_exc()
        _write_json(Path(request["result_path"]), result)
        passed = bool(result.get("all_checks_pass"))
        print(json.dumps({"case_id": request["case"]["case_id"], "status": "ok" if passed else "failed",
                          "case_wall_ns": result.get("case_wall_ns"), "child_exit_code": result.get("child_exit_code")}))
        return 0 if passed else 1
    if args.output is None:
        parser.error("--output is required")
    records = run_migration_suite(args.output, args.profile, args.blocks, args.block_bytes, args.repeats)
    print(json.dumps({"experiment": "migration", "cases": len(records),
                      "failed": sum(record["status"] != "ok" for record in records),
                      "run_directory": records[0]["provenance"]["run_directory"]}))
    return int(any(record["status"] != "ok" for record in records))


if __name__ == "__main__":
    raise SystemExit(_main())
