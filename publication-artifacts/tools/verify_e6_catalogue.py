#!/usr/bin/env python3
"""Check retained E6/E9 inputs and summaries offline, using Python's stdlib.

This program reads existing measurements. It neither runs the archived
experiment programs nor makes fresh performance or security measurements.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from collections import defaultdict


SCHEME_TO_MODE = {
    "B2_full": "full", "B3_leaf": "leaf", "B4_ext": "ext",
    "B4_ext_coalesced": "ext",
}
GROUP_FIELDS = (
    "scale_label", "profile", "budget_fraction", "workload",
    "prediction_error", "algorithm",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def check_files(base, entries):
    for relative, record in entries.items():
        path = base / relative
        require(path.is_file(), "Missing file: " + str(path))
        require(path.stat().st_size == record["bytes"], "Size mismatch: " + relative)
        require(digest(path) == record["sha256"], "SHA-256 mismatch: " + relative)
    return len(entries)


def check_original_input_hashes(base):
    checked = 0
    for line in (base / "original_checksums/component_catalog_inputs.sha256").read_text().splitlines():
        expected, relative = line.split(maxsplit=1)
        require(digest(base / "upstream" / relative) == expected,
                "Original upstream hash mismatch: " + relative)
        checked += 1
    require(checked == 5, "Expected five original upstream input hashes")
    # Keep the original HPC paths as provenance, but resolve each input locally.
    local_names = {
        "component_costs.csv": "component_costs.csv",
        "migration_cost_catalog.csv": "migration_cost_catalog.csv",
    }
    checked_policy = set()
    for line in (base / "original_checksums/policy_input_catalogs.sha256").read_text().splitlines():
        expected, old_path = line.split(maxsplit=1)
        name = Path(old_path).name
        require(name in local_names, "Unexpected original policy input: " + name)
        require(digest(base / local_names[name]) == expected,
                "Original policy input hash mismatch: " + name)
        checked_policy.add(name)
    require(checked_policy == set(local_names), "Incomplete original policy input hashes")
    return checked, len(checked_policy)


def check_catalogue(base, expected):
    catalogue = read_csv(base / "component_costs.csv")
    require(len(catalogue) == expected["component_rows"], "Wrong component catalogue length")
    wanted_keys = {(n, mode) for n in (64, 512, 4096, 16384)
                   for mode in ("full", "leaf", "ext")}
    keys = [(int(row["n"]), row["mode"]) for row in catalogue]
    require(set(keys) == wanted_keys and len(keys) == len(set(keys)),
            "Component catalogue has missing or duplicate parameter points")
    storage, cpu, response = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in read_csv(base / "upstream/E1/theory_vs_serialization.csv"):
        n, mode, epochs = int(row["n"]), row["mode"], int(row["epochs_total"])
        if mode not in ("full", "leaf", "ext") or epochs <= 0:
            continue
        logical = float(row["theoretical_hash_bytes"]) / epochs
        storage[(n, mode)].append(0 if mode == "ext" else math.ceil(logical / 4096) * 4096)
    for row in read_csv(base / "upstream/E2/latency_summary.csv"):
        if (row["ablation"] != "safe" or int(row["block_bytes"]) != 2048
                or row["layout"] != "epoch_packed" or row["codec"] != "raw-v1"):
            continue
        mode = SCHEME_TO_MODE.get(row["scheme"])
        if mode:
            cpu[(int(row["epoch_length"]), mode)].append(float(row["median_ns"]))
    for row in read_csv(base / "upstream/E3/wire_sizes.csv"):
        if row["variant"] == "B4_ext_split" or int(row["block_bytes"]) != 2048:
            continue
        mode = SCHEME_TO_MODE.get(row["variant"]) or SCHEME_TO_MODE.get(row["scheme"])
        if mode:
            response[(int(row["epoch_length_n"]), mode)].append(float(row["total_response_bytes"]))
    available_rows = read_csv(base / "upstream/E7/availability_catalog.csv")
    available = {(int(row["n"]), row["mode"]): float(row["success_probability"])
                 for row in available_rows}
    require(len(available_rows) == len(available) == 12, "Wrong availability catalogue keys")
    checked = 0
    for row in catalogue:
        key = (int(row["n"]), row["mode"])
        for field, samples in (("storage_bytes", storage), ("cpu_ns", cpu),
                               ("response_bytes", response)):
            require(bool(samples.get(key)), "No source values for " + str((key, field)))
            value = statistics.median(samples[key])
            require(value == float(row[field]), "Catalogue value mismatch: " + str((key, field)))
            checked += 1
        require(available[key] == float(row["success_probability"]),
                "Availability mismatch: " + str(key))
        checked += 1
        require(row["publication_eligible"].lower() == "true", "Non-publication catalogue row")
        require(row["energy_joules"] == "", "Unexpected energy measurement in original catalogue")
    migration = read_csv(base / "migration_cost_catalog.csv")
    require(len(migration) == expected["migration_catalogue_rows"], "Wrong migration catalogue length")
    return {"component_rows": len(catalogue), "recomputed_values": checked,
            "migration_catalogue_rows": len(migration), "energy_values_available": False}


def check_paper_cells(summary, expected):
    require(len(summary) == expected["policy_summary_rows"], "Wrong policy summary length")
    cells = [row for row in summary if row["scale_label"] == "primary"
             and row["profile"] == "balanced" and row["workload"] == "nonstationary"
             and float(row["budget_fraction"]) == 0.5 and float(row["prediction_error"]) == 0]
    # Published rounded values, not newly produced benchmark results.
    paper = {
        "dynamic_programming": ("0.092069", 31), "greedy_ratio": ("0.092069", 31),
        "all_ext": ("0.092785", 16), "age_tier": ("1.766165", 197),
        "popularity_tier": ("1.710260", 201), "all_full": ("2.203364", 240),
        "all_leaf": ("2.636006", 256),
    }
    require(len(cells) == 7 and {r["algorithm"] for r in cells} == set(paper),
            "Missing or duplicate rows for the paper's policy table")
    for row in cells:
        objective, migrations = paper[row["algorithm"]]
        require(format(float(row["realised_amortised_objective_median"]), ".6f") == objective,
                "Paper objective mismatch: " + row["algorithm"])
        require(float(row["migration_count_median"]) == migrations,
                "Paper migration count mismatch: " + row["algorithm"])
    dp = next(row for row in cells if row["algorithm"] == "dynamic_programming")
    require(float(dp["mode_full_count_median"]) == 15
            and float(dp["mode_ext_count_median"]) == 241, "Paper mode counts mismatch")
    require(format(100 * float(dp["completion_probability_median"]), ".3f") == "92.253",
            "Paper completion probability mismatch")
    return {"summary_rows": len(summary), "paper_policy_rows": 7,
            "paper_objective_and_migration_cells": 14, "paper_dp_context_values": 3}


def group_key(row):
    return tuple(float(row[name]) if name in ("budget_fraction", "prediction_error")
                 else row[name] for name in GROUP_FIELDS)


def quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def check_run(run_dir, manifest, summary):
    checked_files = check_files(run_dir, manifest["optional_run_files"])
    expected = manifest["expected_counts"]
    component_hash = manifest["files"]["component_costs.csv"]["sha256"]
    migration_hash = manifest["files"]["migration_cost_catalog.csv"]["sha256"]
    groups = defaultdict(list)
    counts = {}
    for kind in ("migration", "policy"):
        with (run_dir / (kind + "_plan.jsonl")).open(encoding="utf-8") as handle:
            planned = [json.loads(line)["case_id"] for line in handle if line.strip()]
        require(len(planned) == len(set(planned)), "Duplicate planned " + kind + " case")
        seen = set()
        raw_paths = sorted((run_dir / ("raw_" + kind)).glob("*.json.gz"))
        require(len(raw_paths) == expected[kind + "_cases"], "Wrong raw " + kind + " file count")
        for path in raw_paths:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                raw = json.load(handle)
            case_id = raw["case_id"]
            require(case_id not in seen, "Duplicate raw case: " + case_id)
            seen.add(case_id)
            require(raw["all_checks_pass"] is True, "Archived check flag is false: " + case_id)
            if kind == "policy":
                require(raw["component_catalog_sha256"] == component_hash,
                        "Wrong component input for " + case_id)
                require(raw["migration_catalog_sha256"] == migration_hash,
                        "Wrong migration input for " + case_id)
                require(raw["component_catalog_publication_eligible"] is True,
                        "Non-publication input for " + case_id)
                for result in raw["results"]:
                    row = dict(raw["case"])
                    row.update(result)
                    groups[group_key(row)].append(row)
        require(seen == set(planned), "Planned and retained " + kind + " IDs differ")
        counts[kind + "_cases"] = len(seen)
    observed = sum(len(rows) for rows in groups.values())
    require(observed == expected["policy_observations"], "Wrong policy observation count")
    summary_keys = [group_key(row) for row in summary]
    require(len(summary_keys) == len(set(summary_keys)), "Duplicate summary group")
    require(set(summary_keys) == set(groups), "Raw policy and summary groups differ")
    metrics = [name[:-7] for name in summary[0] if name.endswith("_median")]
    checked_cells = 0
    for row in summary:
        members = groups[group_key(row)]
        require(int(row["observations"]) == len(members), "Summary observation count mismatch")
        fraction = sum(bool(member["feasible"]) for member in members) / len(members)
        require(fraction == float(row["feasible_fraction"]), "Summary feasibility mismatch")
        checked_cells += 2
        for metric in metrics:
            values = [float(member[metric]) for member in members
                      if member.get(metric) not in (None, "")]
            for suffix, probability in (("median", 0.5), ("p95", 0.95)):
                value = quantile(values, probability)
                recorded = row[metric + "_" + suffix]
                if value is None:
                    require(recorded == "", "Expected empty summary energy value")
                else:
                    require(recorded != "" and math.isclose(value, float(recorded),
                            rel_tol=1e-12, abs_tol=1e-12),
                            "Recomputed summary differs: " + str(group_key(row)) + " " + metric + "_" + suffix)
                checked_cells += 1
    return {"files_hashed": checked_files, **counts, "policy_observations": observed,
            "policy_summary_groups_recomputed": len(groups),
            "policy_summary_cells_recomputed": checked_cells,
            "numeric_tolerance": "relative 1e-12, absolute 1e-12"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path,
                        default=Path(__file__).resolve().parents[1] / "inputs/e6",
                        help="E6 input bundle directory (default: ../inputs/e6)")
    parser.add_argument("--run-dir", type=Path,
                        help="Optional extracted e6-migration-publication-20260625-123230 directory")
    args = parser.parse_args()
    try:
        manifest = json.loads((args.inputs / "manifest.json").read_text(encoding="utf-8"))
        files_hashed = check_files(args.inputs, manifest["files"])
        upstream_hashes, policy_hashes = check_original_input_hashes(args.inputs)
        catalogue = check_catalogue(args.inputs, manifest["expected_counts"])
        summary = read_csv(args.inputs / "policy_summary.csv")
        paper = check_paper_cells(summary, manifest["expected_counts"])
        result = {
            "status": "PASS", "scope": "Offline retained-record consistency; no experiment rerun",
            "bundle_files_hashed": files_hashed, "original_upstream_hashes": upstream_hashes,
            "original_policy_input_hashes": policy_hashes, "catalogue": catalogue,
            "paper_summary": paper,
            "raw_run": (check_run(args.run_dir, manifest, summary) if args.run_dir else
                        {"status": "NOT_REQUESTED", "option": "--run-dir"}),
            "notes": [
                "Internal E9 is the paper's Section VII-F mode-assignment study.",
                "Energy entries are empty; these files provide no energy measurements.",
                "The cpu_ns catalogue uses retained E2 acceptance-latency medians.",
                "These are original-run records, not reruns of later corrected implementations.",
            ],
        }
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
