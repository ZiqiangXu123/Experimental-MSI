





from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
from typing import Any

EXPERIMENTS = {"local", "network", "provider", "storage", "memory_pressure", "original_e2",
               "archive_retrieval", "energy_workload"}
SUCCESS = {"ok", "success", "passed", "complete", "completed", "measured_here"}
GROUP_FIELDS = [
    "experiment", "role", "machine_id", "pi_model", "architecture", "hostname", "data_kind",
    "mode", "scheme", "n", "implementation", "source", "network_kind", "memory_limit_mib",
    "pressure_mib", "dataset_identity", "profile", "workload_seed", "queries", "warmup",
]
PAIR_FIELDS = [
    "pi_role", "other_role", "pi_machine_id", "other_machine_id", "pi_model",
    "pi_architecture", "other_architecture", "other_hostname", "data_kind", "scheme", "n",
    "profile", "workload_seed", "queries", "warmup", "pi_n_trials", "other_n_trials",
    "matched_trial_ids", "pi_median_ms", "other_median_ms", "pi_over_other_ratio",
    "pi_group_id", "other_group_id", "warnings",
]
REPORT_START = "<!-- msi-trial-aggregation:start -->"
REPORT_END = "<!-- msi-trial-aggregation:end -->"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _numeric_metrics(value: Any, prefix: str = "") -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for key, item in _mapping(value).items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            out.update(_numeric_metrics(item, name))
        elif _number(item):
            out[name] = item
        
    return out


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def _source_path(value: Any) -> str:
    
    return os.path.normcase(os.path.abspath(os.path.normpath(str(value))))


def _read_manifest(path: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        warnings.append(f"Could not read run manifest: {type(exc).__name__}: {exc}")
        return {}


def _dimensions(row: dict[str, Any], manifest: dict[str, Any], source_identity: str) -> dict[str, Any]:
    provenance = _mapping(row.get("provenance"))
    measured = _mapping(row.get("metrics"))
    platform = _mapping(row.get("platform")) or _mapping(row.get("system_info"))
    config = _mapping(manifest.get("config"))
    experiment = str(row["experiment"])
    workload_seed = _first(provenance.get("workload_seed"), provenance.get("seed"), manifest.get("seed"))
    selected_hash = _first(provenance.get("selected_payload_sha256"), row.get("selected_payload_sha256"))
    dataset_hash = _first(provenance.get("dataset_sha256"), row.get("dataset_sha256"))
    if selected_hash:
        dataset_identity = f"selected_payload_sha256:{selected_hash}"
    elif dataset_hash:
        dataset_identity = f"dataset_sha256:{dataset_hash}"
    elif experiment == "original_e2" and workload_seed is not None:
        dataset_identity = f"original_seed:{workload_seed}"
    else:
        dataset_identity = f"unidentified_dataset:{source_identity}"
    hostname = _first(platform.get("hostname"), "")
    actual_id = _first(platform.get("actual_machine_id"), platform.get("machine_id"), platform.get("device_id"))
    machine_id = actual_id or (f"hostname:{hostname}" if hostname else f"unidentified_machine:{source_identity}")
    return {
        "experiment": experiment, "role": str(row.get("role", "unspecified")),
        "machine_id": machine_id,
        "pi_model": _first(platform.get("pi_model"), platform.get("device_tree_model"), platform.get("device_model"), ""),
        "architecture": _first(platform.get("architecture"), platform.get("arch"), platform.get("machine"), ""),
        "hostname": hostname, "data_kind": row.get("data_kind", "unspecified"),
        "mode": _first(provenance.get("mode"), row.get("mode"), measured.get("mode"), ""),
        "scheme": _first(provenance.get("scheme"), row.get("scheme"), measured.get("scheme"), ""),
        "n": _first(provenance.get("n"), row.get("n"), measured.get("n")),
        "implementation": _first(provenance.get("implementation"), row.get("implementation"), "unspecified"),
        "source": _first(provenance.get("source"), row.get("source"), "unspecified"),
        "network_kind": _first(provenance.get("network_kind"), "unspecified"),
        "memory_limit_mib": _first(provenance.get("memory_limit_mib"), measured.get("memory_limit_mib")),
        "pressure_mib": _first(provenance.get("pressure_mib"), measured.get("pressure_mib")),
        "dataset_identity": dataset_identity,
        "profile": _first(provenance.get("profile"), manifest.get("profile")),
        "workload_seed": workload_seed,
        "queries": _first(provenance.get("queries"), config.get("queries"), measured.get("queries")),
        "warmup": _first(provenance.get("warmup"), config.get("warmup"), measured.get("warmup")),
    }


def _fixture(row: dict[str, Any]) -> bool:
    value = json.dumps([row.get("data_kind"), row.get("provenance")], sort_keys=True).lower()
    return any(marker in value for marker in ("test_fixture", "unit_test", "unit-test"))


def _expected_trials(row: dict[str, Any], manifest: dict[str, Any]) -> int | None:
    provenance = _mapping(row.get("provenance"))
    explicit = _first(provenance.get("expected_trials"), row.get("expected_trials"))
    
    
    expected = explicit if explicit is not None else (1 if row["experiment"] == "energy_workload"
                                                      else _mapping(manifest.get("config")).get("trials"))
    return expected if isinstance(expected, int) and not isinstance(expected, bool) and expected > 0 else None


def _csv(path: Path, records: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def _pair_groups(groups: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    eligible = []
    for group in groups:
        dimensions = group["dimensions"]
        if dimensions["experiment"] != "original_e2" or dimensions["implementation"] != "original_e2":
            continue
        if not group["items"] or any(_fixture(item["row"]) for item in group["items"]):
            continue
        required = ("scheme", "n", "profile", "workload_seed", "queries", "warmup", "architecture")
        missing = [name for name in required if dimensions.get(name) in (None, "", "unknown", "unspecified")]
        if missing:
            warnings.append(f"Group {group['group_id']}: no platform pair; missing workload metadata {', '.join(missing)}")
            continue
        if str(dimensions["machine_id"]).startswith("unidentified_machine:"):
            warnings.append(f"Group {group['group_id']}: no platform pair; missing machine identity")
            continue
        eligible.append(group)
    pis = [group for group in eligible if "raspberry pi" in str(group["dimensions"]["pi_model"]).lower()]
    others = [group for group in eligible if "raspberry pi" not in str(group["dimensions"]["pi_model"]).lower()]
    pairs = []
    keys = ("scheme", "n", "profile", "workload_seed", "queries", "warmup", "data_kind", "dataset_identity",
            "implementation", "source", "network_kind", "memory_limit_mib", "pressure_mib")
    for pi in pis:
        left = pi["dimensions"]
        for other in others:
            right = other["dimensions"]
            if left["machine_id"] == right["machine_id"] or left["architecture"] == right["architecture"]:
                continue
            if any(left[key] != right[key] for key in keys):
                continue
            pi_trials: dict[str, list[dict[str, Any]]] = defaultdict(list)
            other_trials: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in pi["items"]:
                pi_trials[item["trial"]].append(item)
            for item in other["items"]:
                other_trials[item["trial"]].append(item)
            matched_ids = []
            pi_samples, other_samples = [], []
            pair_warnings = []
            for trial in sorted(set(pi_trials) & set(other_trials)):
                a, b = pi_trials[trial], other_trials[trial]
                
                
                seeds_a = {_json(_mapping(item["row"].get("provenance")).get("seed")) for item in a}
                seeds_b = {_json(_mapping(item["row"].get("provenance")).get("seed")) for item in b}
                if seeds_a != seeds_b:
                    pair_warnings.append(f"Trial {trial}: actual seed mismatch; excluded from pairing")
                    continue
                values_a = [item["metrics"].get("median_ms") for item in a]
                values_b = [item["metrics"].get("median_ms") for item in b]
                if not all(_number(value) for value in values_a + values_b):
                    pair_warnings.append(f"Trial {trial}: missing finite median_ms; excluded from pairing")
                    continue
                matched_ids.append(trial)
                pi_samples.extend(values_a)
                other_samples.extend(values_b)
            unmatched = sorted(set(pi_trials) ^ set(other_trials))
            if unmatched:
                pair_warnings.append(f"Unmatched trial IDs excluded: {', '.join(unmatched)}")
            if not pi_samples or not other_samples:
                warnings.append(f"Groups {pi['group_id']}/{other['group_id']}: no matching measured trials for comparison")
                warnings.extend(pair_warnings)
                continue
            pi_median, other_median = statistics.median(pi_samples), statistics.median(other_samples)
            if other_median <= 0 or pi_median < 0:
                warnings.append(f"Groups {pi['group_id']}/{other['group_id']}: nonpositive denominator or negative latency; ratio omitted")
                continue
            pair = {
                "pi_role": left["role"], "other_role": right["role"],
                "pi_machine_id": left["machine_id"], "other_machine_id": right["machine_id"],
                "pi_model": left["pi_model"], "pi_architecture": left["architecture"],
                "other_architecture": right["architecture"], "other_hostname": right["hostname"],
                **{key: left[key] for key in ("data_kind", "scheme", "n", "profile", "workload_seed", "queries", "warmup")},
                "pi_n_trials": len(pi_samples), "other_n_trials": len(other_samples),
                "matched_trial_ids": _json(matched_ids), "pi_median_ms": pi_median,
                "other_median_ms": other_median, "pi_over_other_ratio": pi_median / other_median,
                "pi_group_id": pi["group_id"], "other_group_id": other["group_id"],
                "warnings": _json(pair_warnings),
            }
            pairs.append(pair)
            warnings.extend(f"Pair {pi['group_id']}/{other['group_id']}: {message}" for message in pair_warnings)
    return pairs


def _append_report(report_path: Path, summary: dict[str, Any]) -> None:
    if not report_path.is_file():
        return
    report = report_path.read_text(encoding="utf-8")
    section = (
        f"{REPORT_START}\n\n"
        "Independent trial tables: [grouped_summary.csv](grouped_summary.csv) and "
        "[paired_platform_comparison.csv](paired_platform_comparison.csv). "
        f"There are {summary['groups']} groups and {summary['platform_pairs']} matched platform pairs. "
        "The grouped values summarize per-trial metrics; query sample arrays are never pooled. "
        "Cross-platform ratios use only matching original E2 workloads and observed machine metadata; "
        "the other machine retains its recorded role. "
        "[Aggregation warnings](aggregation_warnings.json) document excluded duplicate imports, "
        "missing trials, and unmatched comparison conditions.\n\n"
        f"{REPORT_END}"
    )
    pattern = re.compile(re.escape(REPORT_START) + r".*?" + re.escape(REPORT_END), re.S)
    report = pattern.sub(lambda _match: section, report) if pattern.search(report) else report.rstrip() + "\n\n" + section + "\n"
    report_path.write_text(report, encoding="utf-8")


def aggregate(run_dir: Path) -> dict[str, Any]:
    
    run_dir = Path(run_dir).resolve()
    input_path = run_dir / "results.jsonl"
    warnings: list[str] = []
    manifest = _read_manifest(run_dir / "run_manifest.json", warnings)
    sources: dict[str, dict[str, Any]] = {}
    for source in manifest.get("sources", []):
        if isinstance(source, dict) and source.get("path"):
            sources[_source_path(source["path"])] = source
    groups: dict[str, dict[str, Any]] = {}
    input_rows = 0
    excluded_missing_trial = 0
    skipped_rows = 0
    for line_number, line in enumerate(input_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            warnings.append(f"Line {line_number}: invalid JSON excluded ({exc})")
            skipped_rows += 1
            continue
        if not isinstance(row, dict):
            warnings.append(f"Line {line_number}: non-object row excluded")
            skipped_rows += 1
            continue
        input_rows += 1
        if row.get("experiment") not in EXPERIMENTS or str(row.get("status", "")).lower() not in SUCCESS:
            skipped_rows += 1
            continue
        provenance = _mapping(row.get("provenance"))
        source_path = _source_path(provenance.get("source_run", run_dir))
        source_entry = sources.get(source_path, {})
        source_manifest = _mapping(source_entry.get("manifest")) or manifest
        source_identity = str(source_entry.get("results_sha256") or source_path)
        trial_value = _first(provenance.get("trial"), row.get("trial"), row.get("trial_id"))
        if trial_value is None or isinstance(trial_value, (dict, list, bool)):
            warnings.append(f"Line {line_number} ({row.get('case_id', 'unnamed')}): missing independent trial identifier; excluded")
            excluded_missing_trial += 1
            continue
        trial = str(trial_value)
        dimensions = _dimensions(row, source_manifest, source_identity)
        try:
            key = _json(dimensions)
        except (ValueError, TypeError):
            warnings.append(f"Line {line_number}: invalid/nonfinite grouping metadata excluded")
            skipped_rows += 1
            continue
        group = groups.setdefault(key, {"dimensions": dimensions,
            "group_id": hashlib.sha256(key.encode()).hexdigest()[:16],
            "candidates": defaultdict(list), "expected": {}, "warnings": []})
        item = {"row": row, "trial": trial, "source": source_identity,
                "metrics": _numeric_metrics(row.get("metrics")), "line": line_number}
        group["candidates"][(source_identity, trial)].append(item)
        expected = _expected_trials(row, source_manifest)
        if expected is not None:
            group["expected"][source_identity] = expected
    output_rows = []
    duplicate_trials_excluded = 0
    conflicting_trials_excluded = 0
    processed_groups = []
    for key in sorted(groups):
        group = groups[key]
        items = []
        duplicate_count = 0
        conflict_count = 0
        for (source, trial), candidates in sorted(group["candidates"].items()):
            
            
            payloads = {json.dumps({"metrics": item["row"].get("metrics", {}),
                                   "seed": _mapping(item["row"].get("provenance")).get("seed")},
                                  sort_keys=True, ensure_ascii=False, allow_nan=True)
                        for item in candidates}
            if len(payloads) > 1:
                conflict_count += len(candidates)
                group["warnings"].append(f"Conflicting duplicate source trial {trial} ({source}); all {len(candidates)} records excluded")
                continue
            items.append(candidates[0])
            if len(candidates) > 1:
                removed = len(candidates) - 1
                duplicate_count += removed
                group["warnings"].append(f"Duplicate source trial {trial} ({source}): {removed} repeated record(s) excluded")
        if str(group["dimensions"]["machine_id"]).startswith(("unidentified_machine:", "hostname:")):
            group["warnings"].append("No actual machine ID: grouping uses the explicitly reported hostname or source identity")
        if str(group["dimensions"]["dataset_identity"]).startswith("unidentified_dataset:"):
            group["warnings"].append("Missing payload/dataset hash: data identity is isolated to the source run")
        for source, expected in sorted(group["expected"].items()):
            trial_ids = {item["trial"] for item in items if item["source"] == source}
            missing = sorted(set(map(str, range(expected))) - trial_ids)
            if missing:
                group["warnings"].append(f"Missing trials from {source}: {', '.join(missing)} (expected {expected}; observed {len(trial_ids)})")
        if not group["expected"]:
            group["warnings"].append("Expected independent-trial count not recorded; missing-trial coverage cannot be established")
        metrics: dict[str, list[int | float]] = defaultdict(list)
        for item in items:
            for name, value in item["metrics"].items():
                metrics[name].append(value)
        output = {"group_id": group["group_id"], **group["dimensions"], "n_trials": len(items),
                  "duplicate_trials_excluded": duplicate_count, "conflicting_trials_excluded": conflict_count}
        for name, values in sorted(metrics.items()):
            output.update({f"{name}_median": statistics.median(values), f"{name}_min": min(values),
                           f"{name}_max": max(values), f"{name}_count": len(values)})
            if len(values) != len(items):
                group["warnings"].append(f"Metric {name} missing from {len(items) - len(values)} retained trial(s)")
        output["warnings"] = _json(group["warnings"])
        output_rows.append(output)
        group["items"] = items
        processed_groups.append(group)
        duplicate_trials_excluded += duplicate_count
        conflicting_trials_excluded += conflict_count
        warnings.extend(f"Group {group['group_id']}: {message}" for message in group["warnings"])
    paired_rows = _pair_groups(processed_groups, warnings)
    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    base_fields = ["group_id", *GROUP_FIELDS, "n_trials", "duplicate_trials_excluded", "conflicting_trials_excluded", "warnings"]
    numeric_fields = sorted({name for row in output_rows for name in row if name not in base_fields})
    grouped_path = report_dir / "grouped_summary.csv"
    paired_path = report_dir / "paired_platform_comparison.csv"
    _csv(grouped_path, output_rows, base_fields + numeric_fields)
    _csv(paired_path, paired_rows, PAIR_FIELDS)
    summary = {"input_rows": input_rows, "groups": len(output_rows), "platform_pairs": len(paired_rows),
               "retained_trial_rows": sum(row["n_trials"] for row in output_rows),
               "duplicate_trials_excluded": duplicate_trials_excluded,
               "conflicting_trials_excluded": conflicting_trials_excluded,
               "missing_trial_rows_excluded": excluded_missing_trial, "skipped_rows": skipped_rows,
               "grouped_summary_csv": str(grouped_path), "paired_platform_comparison_csv": str(paired_path),
               "results_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
               "warnings": sorted(set(warnings)),
               "summary_scope": "independent trial-level scalar metrics; no pooled query samples",
               "comparison_scope": "ratios of platform medians over shared original E2 trial IDs and matching workload metadata; no inferred hardware role"}
    (report_dir / "aggregation_warnings.json").write_text(_json(summary) + "\n", encoding="utf-8")
    _append_report(report_dir / "report.md", summary)
    return summary
