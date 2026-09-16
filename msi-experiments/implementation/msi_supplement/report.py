





from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SUCCESS = {"measured_here", "ok", "success", "passed", "complete", "completed"}
FAILURE = {"failed", "failure", "error", "timeout", "invalid"}
PLANNED = ("original_e2", "local", "archive_retrieval", "network", "storage", "provider", "migration", "integrity",
           "memory_pressure", "disconnect", "energy_workload", "energy", "real_dataset", "native_finality")
COMMENTS = (
    ("R1-1", "Storage accounting and provider-side overhead",
     ("storage_accounting", "provider_latency", "filesystem")),
    ("R1-2", "IoT deployment and blockchain integration scope",
     ("real_pi_platform", "remote_tcp", "memory_pressure", "energy", "filesystem", "service_disconnect", "native_finality")),
    ("R2-1", "Evaluation on constrained hardware",
     ("real_pi_platform", "memory_pressure")),
    ("R2-2", "CPU, RAM and latency measurements",
     ("cpu", "ram", "local_latency")),
    ("R2-3", "Presentation of experimental evidence",
     ("presentation",)),
    ("R2-4", "Reproducible repository, real data and hardware evidence",
     ("repository_metadata", "real_data", "real_pi_platform")),
    ("R2-8", "Real-world benchmarking",
     ("real_pi_platform", "real_data")),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _flatten(value: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict) and item:
            out.update(_flatten(item, name))
        else:
            out[name] = _json(item) if isinstance(item, (dict, list)) else item
    return out


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _metrics(row: dict) -> dict:
    value = row.get("metrics", {})
    return _flatten(value) if isinstance(value, dict) else {}


def _metric(row: dict, names: Iterable[str]) -> tuple[str, float] | None:
    metrics = _metrics(row)
    for name in names:
        if _number(metrics.get(name)):
            return name, float(metrics[name])
    return None


def _any_metric(rows: list[dict], pattern: str) -> bool:
    return any(re.search(pattern, key, re.I) and _number(value)
               for row in rows for key, value in _metrics(row).items())


def _successful(row: dict) -> bool:
    return str(row.get("status", "")).lower() in SUCCESS and any(
        _number(value) or isinstance(value, bool) for value in _metrics(row).values())


def _fixture(row: dict) -> bool:
    values = (row.get("data_kind", ""), row.get("provenance", {}))
    return bool(re.search(r"test_fixture|unit_test|unit-test", _json(values), re.I))


def _pi_system(value: Any) -> bool:
    
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if key in {"device_model", "model", "device_tree_model", "pi_model"} and isinstance(item, str):
            if "raspberry pi" in item.lower():
                return True
        if isinstance(item, dict) and _pi_system(item):
            return True
    return False


def _remote(row: dict) -> bool:
    provenance = row.get("provenance", {})
    if not isinstance(provenance, dict):
        return False
    flat = _flatten(provenance)
    transport = " ".join(str(v).lower() for k, v in flat.items()
                         if any(t in k for t in ("transport", "protocol", "network_mode", "network_kind")))
    if "tcp" not in transport or "loopback" in transport:
        return False
    host_values = [str(v).lower() for k, v in flat.items()
                   if k in {"host", "server_host", "remote_host", "provider_host"}]
    if any(re.match(r"(?:\w+://)?(?:localhost(?::|$)|127\.|\[?::1(?:\]|$))", v) for v in host_values):
        return False
    pairs = (
        ("client_hostname", "server_hostname"), ("local_hostname", "remote_hostname"),
        ("client_device_id", "server_device_id"), ("local_device_id", "remote_device_id"),
        ("client_machine_id", "server_machine_id"),
        ("client_system.hostname", "server_system.hostname"),
        ("client_system_info.hostname", "server_system_info.hostname"),
        ("client_system_info.device_id", "server_system_info.device_id"),
        ("system_info.hostname", "server_system_info.hostname"),
        ("system_info.device_id", "server_system_info.device_id"),
    )
    return any(flat.get(a) and flat.get(b) and str(flat[a]) != str(flat[b]) for a, b in pairs)


def _evidence(rows: list[dict], manifest: dict) -> dict[str, dict]:
    measured = [row for row in rows if _successful(row)]
    physical = [row for row in measured if not _fixture(row)]
    groups = {name: [r for r in measured if r.get("experiment") == name] for name in PLANNED}
    real_physical = [r for r in physical if str(r.get("data_kind", "")).lower()
                     in {"real", "real_data", "real_chain", "real_chain_data", "public_chain",
                         "ethereum_rpc", "ethereum_rpc_replay", "ethereum_rpc_finalized_json", "real_world", "realworld"}]
    
    real_data = any(re.search(r"source|rpc|chain_id|origin", _json(r.get("provenance", {})), re.I)
                    and re.search(r"sha256|hash", _json(r.get("provenance", {})), re.I)
                    for r in real_physical)
    def row_system(row: dict) -> dict:
        provenance = row.get("provenance", {})
        provenance = provenance if isinstance(provenance, dict) else {}
        for value in (row.get("system_info"), row.get("platform"), provenance.get("system_info")):
            if isinstance(value, dict) and value:
                return value
        return manifest.get("system_info", manifest.get("system", {}))
    pi = any(_pi_system(row_system(row)) for row in physical)
    remote = any(_remote(r) for r in physical if r.get("experiment") in {"network", "provider", "integrity"})
    energy_rows = [r for r in physical if r.get("experiment") == "energy"]
    energy = _any_metric(energy_rows, r"(^|\.)(energy_j|energy_joules|measured_joules|joules|net_energy_j|total_board_energy_j)$")
    filesystem = _any_metric(groups["storage"], r"allocated.*bytes|filesystem.*bytes|disk.*bytes|physical.*bytes")
    pressure_rows = [r for r in physical if r.get("experiment") == "memory_pressure"]
    pressure = bool(pressure_rows) and any(
        re.search(r"limit|pressure|cgroup|rlimit", _json(r.get("provenance", {})), re.I)
        or _any_metric([r], r"limit.*bytes|pressure.*bytes") for r in pressure_rows)
    disconnect = any(_remote(r) and re.search(r"disconnect|service_kill|service_stop", _json(r), re.I)
                     and any(v is True or (_number(v) and v > 0) for k, v in _metrics(r).items()
                             if re.search(r"detect|reject|expected|failure|disconnect", k, re.I))
                     for r in physical)
    checks = {
        "real_pi_platform": (pi, "Successful measurement with a Raspberry Pi device model in system_info.",
                             "No successful measurement is backed by a Raspberry Pi system device model; a platform label or desktop smoke run is insufficient."),
        "remote_tcp": (remote, "TCP measurement records distinct client and server machine identities.",
                       "No successful TCP measurement records distinct client/server hostnames or device IDs; loopback does not establish a remote deployment."),
        "real_data": (real_data, "Real-data measurements record source metadata and an input hash.",
                      "No successful real-data measurement includes source metadata and an input hash; synthetic/test data do not establish real-world workloads."),
        "energy": (energy, "An energy experiment records numeric joule measurements.",
                   "No measured energy in joules is present; CPU time and estimated power are not energy measurements."),
        "filesystem": (filesystem, "Storage experiment includes actual allocated/filesystem byte measurements.",
                       "No allocated/filesystem-byte measurement is present; analytical byte accounting does not measure filesystem usage."),
        "memory_pressure": (pressure, "Memory-pressure experiment records an enforced limit or pressure configuration.",
                            "No successful memory-pressure experiment records a limit or pressure configuration."),
        "service_disconnect": (disconnect, "Remote TCP service-disconnection experiment records observed detection or failure.",
                               "No observed service-disconnection test on distinct TCP hosts is present; in-process fault injection does not establish this evidence."),
        "native_finality": (False, "", "Native finality and sharded-consensus integration are absent. Ethereum RPC JSON replay is an ingestion/verification adapter, not native finality validation or consensus integration."),
        "storage_accounting": (_any_metric(groups["storage"], r"verifier.*bytes") and
                               _any_metric(groups["storage"], r"provider.*bytes"),
                               "Verifier and provider byte accounting is available.",
                               "Separate verifier and provider byte accounting is missing."),
        "provider_latency": (_any_metric(groups["provider"], r"(latency|median|p95).*ms|ms.*(median|p95)"),
                             "Provider latency is measured.", "Provider latency measurements are missing."),
        "cpu": (_any_metric(measured, r"cpu.*(seconds|percent|pct|_s$|_ms|_ns)|process_time"),
                "CPU-time or CPU-utilization metrics are present.", "CPU-time/utilization measurements are missing."),
        "ram": (_any_metric(measured, r"rss|ram.*bytes|peak.*memory|memory.*peak"),
                "Resident/peak memory metrics are present.", "Resident/peak memory measurements are missing."),
        "local_latency": (_any_metric(groups["local"], r"(latency|median|p95).*ms|ms.*(median|p95)"),
                          "Local latency metrics are present.", "Local latency measurements are missing."),
        "presentation": (bool(rows), "This run provides machine-readable tables, escaped LaTeX and an offline report; manuscript revision and reviewer acceptance are not assessed.",
                         "No result records are available to present."),
        "repository_metadata": (bool(manifest.get("version") or manifest.get("git_commit") or manifest.get("suite_version"))
                                and bool(manifest.get("config")),
                                "Run metadata records a code version and configuration; public repository availability is not verified.",
                                "A code version and run configuration are not both recorded; public repository availability is not verified."),
    }
    return {key: {"status": "measured_here" if ok else "missing", "reason": yes if ok else no}
            for key, (ok, yes, no) in checks.items()}


def _csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _tex(value: Any) -> str:
    escapes = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
               "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(escapes.get(c, c) for c in str(value)).replace("\n", " ").replace("\r", " ")


def _md(value: Any) -> str:
    return html.escape(str(value)).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _label(row: dict, index: int) -> str:
    provenance = row.get("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
    platform = row.get("platform", "unknown platform")
    if isinstance(platform, dict):
        platform = "/".join(str(platform[key]) for key in ("pi_model", "device_model", "architecture", "hostname") if platform.get(key)) or _json(platform)
    parts = [row.get("case_id", "unknown"), platform,
             row.get("data_kind", "unknown data"), row.get("mode", provenance.get("mode", "mode unspecified")),
             f"trial {row.get('trial', row.get('trial_id', provenance.get('trial', 'unspecified')))}", f"row {index}"]
    dataset = row.get("dataset", provenance.get("dataset_id", provenance.get("dataset_sha256")))
    if dataset:
        parts.insert(3, f"dataset {_json(dataset) if isinstance(dataset, dict) else dataset}")
    if _fixture(row):
        parts.append("test_fixture")
    return " | ".join(map(str, parts))


def _svg(path: Path, title: str, unit: str, series: list[tuple[str, list[tuple[str, float]]]]) -> None:
    
    width, left, right = 1280, 20, 1180
    wrapped = [(textwrap.wrap(label, width=165, break_long_words=True) or [""], values) for label, values in series]
    heights = [len(lines) * 14 + len(values) * 20 + 25 for lines, values in wrapped]
    height = 100 + sum(heights)
    max_value = max((v for _, values in series for _, v in values), default=0)
    scale = 880 / max_value if max_value > 0 else 0
    palette = ("#1b6b9b", "#c06122", "#357d48", "#7953a2")
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
             f'<title>{html.escape(title)}</title>', '<rect width="100%" height="100%" fill="white"/>',
             '<g font-family="sans-serif" fill="#17212b">',
             f'<text x="20" y="27" font-size="18">{html.escape(title)}</text>',
             f'<text x="20" y="49" font-size="12">{html.escape(unit)}; zero-based bars; individual records; no pooled trials</text>']
    for fraction in (0, .25, .5, .75, 1):
        x = 250 + 880 * fraction
        parts.append(f'<text x="{x}" y="68" font-size="10">{max_value * fraction:.4g}</text>')
    y = 87
    for (lines, values), row_h in zip(wrapped, heights):
        for line_no, line in enumerate(lines):
            parts.append(f'<text x="{left}" y="{y + line_no * 14}" font-size="10">{html.escape(line)}</text>')
        for j, (name, value) in enumerate(values):
            by = y + len(lines) * 14 + j * 20
            parts.extend((f'<rect x="250" y="{by}" width="{value * scale:.3f}" height="13" fill="{palette[j % len(palette)]}"/>',
                          f'<text x="20" y="{by + 11}" font-size="11">{html.escape(name)}</text>',
                          f'<text x="{min(250 + value * scale + 6, right)}" y="{by + 11}" font-size="11">{value:.6g}</text>'))
        y += row_h
    parts.append("</g></svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _charts(rows: list[dict], out: Path, warnings: list[str]) -> list[str]:
    specs = (
        ("local", "local_latency.svg", "Local query latency", "milliseconds", (
            ("median", ("median_ms", "latency_median_ms", "latency_ms.median", "latency.median_ms", "latency_ms.p50", "query_median_ms")),
            ("p95", ("p95_ms", "latency_p95_ms", "latency_ms.p95", "latency.p95_ms", "query_p95_ms")))),
        ("storage", "storage_accounting.svg", "Verifier and provider storage", "bytes", (
            ("verifier", ("verifier_total_bytes", "verifier_bytes", "verifier_resident_bytes", "verifier_logical_bytes", "verifier.total_bytes", "verifier.logical_bytes")),
            ("provider", ("provider_bytes", "provider_total_bytes", "provider_logical_bytes", "provider.total_bytes", "provider.logical_bytes")))),
        ("provider", "provider_latency.svg", "Provider service latency", "milliseconds", (
            ("median", ("median_ms", "latency_median_ms", "latency_ms.median", "latency.median_ms", "provider_median_ms", "service_median_ms")),
            ("p95", ("p95_ms", "latency_p95_ms", "latency_ms.p95", "latency.p95_ms", "provider_p95_ms", "service_p95_ms")))),
    )
    generated = []
    for experiment, filename, title, unit, wanted in specs:
        series = []
        seen: set[str] = set()
        for index, row in enumerate(rows, 1):
            if row.get("experiment") != experiment or not _successful(row):
                continue
            fingerprint = _json(row)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            values = []
            for name, aliases in wanted:
                metric = _metric(row, aliases)
                if metric and metric[1] >= 0:
                    values.append((f"{name} ({metric[0]})", metric[1]))
            if values:
                series.append((_label(row, index), values))
        if series:
            _svg(out / filename, title, unit, series)
            generated.append(filename)
    return generated


def analyze(run_dir: Path) -> Path:
    
    run_dir = Path(run_dir)
    out = run_dir / "report"
    out.mkdir(parents=True, exist_ok=True)
    source = run_dir / "results.jsonl"
    warnings: list[str] = []
    rows: list[dict] = []
    malformed = 0
    hashes: dict[str, Any] = {}
    if source.is_file():
        raw = source.read_bytes()
        hashes["results.jsonl"] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record must be an object")
                if not isinstance(row.get("metrics", {}), dict):
                    raise ValueError("metrics must be an object")
                rows.append(row)
            except (ValueError, TypeError) as exc:
                malformed += 1
                warnings.append(f"Malformed results.jsonl line {lineno}: {exc}")
    else:
        warnings.append("Missing results.jsonl; no measurements were loaded.")
    manifest = {}
    manifest_path = run_dir / "run_manifest.json"
    if manifest_path.is_file():
        raw = manifest_path.read_bytes()
        hashes["run_manifest.json"] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("manifest must be an object")
            manifest = parsed
        except (ValueError, UnicodeDecodeError) as exc:
            warnings.append(f"Invalid run_manifest.json: {exc}")
    else:
        warnings.append("Missing run_manifest.json; configuration, system and version provenance may be incomplete.")
    flattened = [_flatten(row) for row in rows]
    identity = ["experiment", "case_id", "role", "platform", "data_kind", "status"]
    fields = identity + sorted(set().union(*(r.keys() for r in flattened)) - set(identity)) if flattened else identity
    _csv(out / "summary.csv", flattened, fields)
    duplicates = sum(count - 1 for count in Counter(_json(r) for r in rows).values())
    if duplicates:
        warnings.append(f"{duplicates} exact duplicate record(s) retained in summary.csv and excluded from duplicate chart entries; do not count them as independent trials.")
    groups: dict[str, set[str]] = {}
    for row in rows:
        if not _successful(row):
            continue
        provenance = row.get("provenance", {})
        if not isinstance(provenance, dict):
            provenance = {}
        group = _json([row.get(k) for k in ("experiment", "case_id", "role", "platform", "data_kind", "mode", "dataset")]
                      + [provenance.get(k) for k in ("mode", "dataset_id", "dataset_sha256")])
        trial = row.get("trial_id", row.get("trial", provenance.get("trial_id", provenance.get("trial"))))
        if trial is not None:
            groups.setdefault(group, set()).add(str(trial))
        else:
            groups.setdefault(group, set())
    for group, trials in groups.items():
        if len(trials) < 3:
            warnings.append(f"Insufficient independent trials: {len(trials)} identified for {group}; at least 3 recommended. Per-query samples do not substitute for independent trials.")
    statuses = Counter(str(row.get("status", "missing")) for row in rows)
    failures = sum(count for status, count in statuses.items() if status.lower() in FAILURE)
    if failures:
        warnings.append(f"{failures} failed measurement record(s); failure rows are retained and are not positive evidence.")
    if not rows:
        warnings.append("No valid result records; all experimental evidence is missing.")
    evidence = _evidence(rows, manifest)
    coverage = []
    for comment_id, scope, required in COMMENTS:
        available = [key for key in required if evidence[key]["status"] == "measured_here"]
        missing = [key for key in required if key not in available]
        status = "measured_here" if not missing else "partial" if available else "missing"
        
        if comment_id == "R2-4" and status == "measured_here":
            status = "partial"
            missing.append("public_repository_availability_unverified")
        coverage.append({"comment_id": comment_id, "whole_comment_scope": scope, "status": status,
                         "available_evidence": "; ".join(available), "missing_evidence": "; ".join(missing),
                         "limitation": "Evidence coverage in this run only; does not assert reviewer acceptance or manuscript revision."})
    _csv(out / "reviewer_coverage.csv", coverage,
         ["comment_id", "whole_comment_scope", "status", "available_evidence", "missing_evidence", "limitation"])
    (out / "missing_evidence.json").write_text(_json(evidence) + "\n", encoding="utf-8")
    (out / "input_hashes.json").write_text(_json(hashes) + "\n", encoding="utf-8")
    
    for name in ("local_latency.svg", "storage_accounting.svg", "provider_latency.svg"):
        (out / name).unlink(missing_ok=True)
    charts = _charts(rows, out, warnings)
    totals = {"valid_records": len(rows), "successful_records": sum(_successful(r) for r in rows),
              "failed_records": failures, "malformed_lines": malformed, "exact_duplicate_records": duplicates,
              "statuses": dict(statuses), "warnings": warnings}
    (out / "report_metadata.json").write_text(_json(totals) + "\n", encoding="utf-8")
    config = manifest.get("config", {})
    config = config if isinstance(config, dict) else {}
    profile = manifest.get("profile", config.get("profile", "unspecified"))
    smoke = str(profile).lower() == "smoke" or any(str(r.get("provenance", {}).get("profile", "")).lower() == "smoke"
                                                 for r in rows if isinstance(r.get("provenance", {}), dict))
    classification = ("Run classification: integration smoke; these results check execution paths and are not publication evidence. "
                      if smoke else f"Recorded run profile: {profile}. ")
    scope_text = (classification + "Results describe the measured platform and dataset recorded on each row. Desktop and loopback smoke runs "
                  "are not Raspberry Pi or remote-host evidence. Synthetic fixtures are not real chain workloads. "
                  "Ethereum RPC JSON replay does not validate native finality or integrate sharded consensus. "
                  "Verifier-local offloading is not compression or a system-wide storage saving; provider state must be counted separately.")
    md = ["# Supplementary experiment report", "", scope_text, "",
          f"Valid records: {len(rows)}. Successful records: {totals['successful_records']}. Failed records: {failures}. Malformed lines: {malformed}.",
          "", "## Reviewer coverage", "", "Exactly seven whole-comment rows are retained. Statuses describe this run's evidence, not acceptance of the manuscript.", "",
          "| Comment | Whole-comment scope | Status | Missing evidence |", "|---|---|---|---|"]
    md.extend("| " + " | ".join(_md(r[k]) for k in ("comment_id", "whole_comment_scope", "status", "missing_evidence")) + " |" for r in coverage)
    md.extend(["", "## Evidence and limitations", ""])
    md.extend(f"- **{_md(k)}: {v['status']}** — {_md(v['reason'])}" for k, v in evidence.items())
    md.extend(["", "## Experiment inventory", "", "| Experiment | Records | Successful |", "|---|---:|---:|"])
    inventory = [{"experiment": exp, "records": sum(r.get("experiment") == exp for r in rows),
                  "successful": sum(r.get("experiment") == exp and _successful(r) for r in rows)} for exp in PLANNED]
    md.extend(f"| {i['experiment']} | {i['records']} | {i['successful']} |" for i in inventory)
    md.extend(["", "## Figures", "", "Individual records are plotted without pooling cases, platforms, datasets or modes. Median and p95 are within-record metrics; no confidence intervals are inferred from summary statistics.", ""])
    md.extend(f"![{name}]({name})\n" for name in charts)
    if not charts:
        md.append("No compatible numeric data are available for the optional figures.")
    md.extend(["", "## Warnings", ""] + [f"- {_md(w)}" for w in warnings])
    md.extend(["", "## Reproducibility", "", "Input SHA-256 values are in `input_hashes.json`; complete flattened values and provenance are in `summary.csv`. Input rows, including failures, remain separate. `tables.tex` uses plain LaTeX tabular environments and requires no external package.", "", "```json", json.dumps(hashes, indent=2), "```", ""])
    (out / "report.md").write_text("\n".join(md), encoding="utf-8")
    def table(data: list[dict], columns: list[str]) -> str:
        return "<table><thead><tr>" + "".join(f"<th>{html.escape(k)}</th>" for k in columns) + "</tr></thead><tbody>" + "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(row.get(k, '')))}</td>" for k in columns) + "</tr>" for row in data) + "</tbody></table>"
    html_parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
                  '<title>Supplementary experiment report</title><style>body{font:15px/1.5 system-ui,sans-serif;color:#17212b;max-width:1280px;margin:2rem auto;padding:0 1rem}table{border-collapse:collapse;display:block;overflow:auto;margin:1rem 0}th,td{border:1px solid #cbd5df;padding:.45rem;text-align:left;vertical-align:top}th{background:#edf3f7}img{width:100%;height:auto}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:.4rem 0}</style>',
                  '<h1>Supplementary experiment report</h1>', f'<p>{html.escape(scope_text)}</p>',
                  f'<p>Valid records: {len(rows)}. Successful: {totals["successful_records"]}. Failed: {failures}. Malformed lines: {malformed}.</p>',
                  '<h2>Reviewer coverage</h2><p>Exactly seven whole-comment rows; evidence status does not assert reviewer acceptance.</p>',
                  table(coverage, ["comment_id", "whole_comment_scope", "status", "available_evidence", "missing_evidence"]),
                  '<h2>Evidence and limitations</h2>', table([{"evidence": k, **v} for k, v in evidence.items()], ["evidence", "status", "reason"]),
                  '<h2>Experiment inventory</h2>', table(inventory, ["experiment", "records", "successful"]),
                  '<h2>Figures</h2><p>Individual records; no pooling or inferred confidence intervals.</p>']
    html_parts.extend(f'<figure><img src="{name}" alt="{html.escape(name)}"></figure>' for name in charts)
    html_parts.extend(['<h2>Result records</h2>', table(flattened, fields), '<h2>Warnings</h2><ul>'])
    html_parts.extend(f'<li>{html.escape(w)}</li>' for w in warnings)
    html_parts.extend(['</ul><h2>Input hashes</h2>', f'<pre>{html.escape(json.dumps(hashes, indent=2))}</pre>',
                       '<p>See summary.csv, reviewer_coverage.csv, missing_evidence.json and tables.tex for complete exports.</p></html>'])
    (out / "report.html").write_text("\n".join(html_parts), encoding="utf-8")
    tex = ["% Generated from individual result records; no inferred or pooled measurements.", r"\begin{tabular}{p{.08\linewidth}p{.22\linewidth}p{.12\linewidth}p{.40\linewidth}}",
           r"Comment & Scope & Status & Missing evidence \\", r"\hline"]
    tex.extend(" & ".join(_tex(row[k].replace("_", " ") if k == "missing_evidence" else row[k])
                          for k in ("comment_id", "whole_comment_scope", "status", "missing_evidence")) + r" \\" for row in coverage)
    tex.extend([r"\end{tabular}", "", "% Per-record metrics (long labels may require the document author's chosen layout).", r"\begin{tabular}{lllll}",
                r"Experiment/case & Platform/data & Status & Metric & Value \\", r"\hline"])
    for row in rows:
        metrics = _metrics(row)
        for name, value in metrics.items():
            tex.append(" & ".join(_tex(v) for v in (f"{row.get('experiment', '')}/{row.get('case_id', '')}",
                                                    f"{row.get('platform', '')}/{row.get('data_kind', '')}", row.get("status", ""), name, value)) + r" \\")
    tex.extend([r"\end{tabular}", ""])
    (out / "tables.tex").write_text("\n".join(tex), encoding="utf-8")
    from .aggregate import aggregate
    aggregate(run_dir)
    return out
