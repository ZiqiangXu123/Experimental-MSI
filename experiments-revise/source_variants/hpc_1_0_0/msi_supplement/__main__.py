

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import unittest

from . import __version__


SUITE_ROOT = Path(__file__).resolve().parents[1]


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def _port(value: str) -> int:
    number = _positive(value)
    if number > 65535:
        raise argparse.ArgumentTypeError("must be at most 65535")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m msi_supplement",
        description="Run reproducible supplementary MSI experiments and analyze measured outputs.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Show host details and check the original OpenSSL crypto backend")

    token = commands.add_parser("token", help="Create a private provider token file without overwriting")
    token.add_argument("--out", type=Path, required=True)

    serve = commands.add_parser("serve", help="Run the authenticated laboratory provider service")
    serve.add_argument("--data-root", type=Path, required=True)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=_port, default=8765)

    dataset = commands.add_parser("dataset", help="Create or ingest a replay dataset")
    datasets = dataset.add_subparsers(dest="dataset_kind", required=True)
    synthetic = datasets.add_parser("synthetic", help="Generate deterministic synthetic payloads")
    synthetic.add_argument("--out", type=Path, required=True)
    synthetic.add_argument("--epochs", type=_positive, default=1)
    synthetic.add_argument("--blocks", type=_positive, default=4096,
                           help="Blocks per epoch (default: 4096)")
    synthetic.add_argument("--block-bytes", type=_positive, default=2048)
    synthetic.add_argument("--seed", type=int, default=2026)
    ethereum = datasets.add_parser("ethereum", help="Fetch trusted-RPC finalized full block JSON snapshots")
    ethereum.add_argument("--out", type=Path, required=True)
    ethereum.add_argument("--count", type=_positive, default=128,
                          help="Contiguous blocks, at most 10000 (default: 128)")
    ethereum.add_argument("--start", type=_nonnegative,
                          help="First height; by default the range ends at the RPC finalized head")
    ethereum.add_argument("--rpc-env", default="ETH_RPC_URL",
                          help="Name of an environment variable containing the endpoint; never the URL")
    ethereum.add_argument("--timeout", type=float, default=30,
                          help="Timeout per RPC request, at most 300 seconds (default: 30)")

    run = commands.add_parser("run", help="Run a smoke or paper profile and write measurements")
    run.add_argument("--role", choices=("pi-verifier", "hpc"), required=True)
    run.add_argument("--profile", choices=("smoke", "paper"), default="smoke")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--provider", help="Optional provider base URL, without credentials")
    run.add_argument("--token-file", type=Path)
    run.add_argument("--dataset", type=Path)
    run.add_argument("--network-kind", choices=("ethernet", "wifi", "unspecified"), default="unspecified")
    run.add_argument("--seed", type=int, default=2026)
    run.add_argument("--sizes", nargs="+", type=_positive,
                     help="Override profile block counts (each 1..16384)")
    run.add_argument("--trials", type=_positive,
                     help="Override profile trials (1..30)")
    run.add_argument("--queries", type=_positive,
                     help="Override profile measured query count (1..100000)")
    run.add_argument("--skip-migration", action="store_true")
    run.add_argument("--skip-original", action="store_true")

    report = commands.add_parser("report", help="Regenerate tables and an offline report from a run")
    report.add_argument("run_dir", metavar="RUN_DIR", type=Path)
    compare = commands.add_parser("compare", help="Combine run records with their original provenance")
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("run_dirs", metavar="RUN_DIR", nargs="+", type=Path)

    energy = commands.add_parser("energy", help="Attach measured external power CSV using recorded run/idle windows")
    energy.add_argument("--run-dir", type=Path, required=True)
    energy.add_argument("--csv", type=Path, required=True)
    commands.add_parser("test", help="Run offline unit tests; local fixtures do not require external services")
    return parser


def _doctor() -> int:
    from . import metrics

    output = {"suite_version": __version__, "system_info": metrics.system_info()}
    try:
        from . import core
        backend = core.Ed25519OpenSSL()
        seed, message = bytes(range(32)), b"msi-supplement-doctor-v1"
        public = backend.public_from_seed(seed)
        signature = backend.sign(seed, message)
        passed = backend.verify(public, message, signature)
        output["original_crypto"] = {
            "available": True, "implementation": "unchanged original E2 Ed25519OpenSSL",
            "libcrypto": backend.library, "openssl_version": backend.version(),
            "sign_verify_self_check": passed,
        }
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        output["original_crypto"] = {"available": False, "error": str(exc)}
        passed = False
    print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
    return 0 if passed else 1


def _serve(args: argparse.Namespace) -> int:
    from .service import ExperimentServer, read_token

    token = read_token(args.token_file)
    with ExperimentServer((args.host, args.port), args.data_root, token) as server:
        print(f"Provider listening on {args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print("Provider stopped.", file=sys.stderr)
    return 0


def _energy(args: argparse.Namespace) -> int:
    from . import metrics, report

    run_dir = args.run_dir
    windows_path = run_dir / "energy_windows.json"
    windows_raw = windows_path.read_bytes()
    windows = json.loads(windows_raw)
    if not isinstance(windows, list) or not windows or not all(isinstance(value, dict) for value in windows):
        raise ValueError("No sustained energy windows are available; use a paper run with recorded energy workloads")
    manifest = json.loads((run_dir / "run_manifest.json").read_bytes())
    results_path = run_dir / "results.jsonl"
    if not isinstance(manifest, dict) or not results_path.is_file():
        raise ValueError("Energy attachment requires an existing run manifest and results.jsonl")
    source_rows = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("results.jsonl contains a non-object record")
        source_rows.append(row)
    csv_hash = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    windows_hash = hashlib.sha256(windows_raw).hexdigest()
    for row in source_rows:
        provenance = row.get("provenance", {})
        if (row.get("experiment") == "energy" and isinstance(provenance, dict) and
                provenance.get("meter_csv_sha256") == csv_hash and
                provenance.get("energy_windows_sha256") == windows_hash):
            raise ValueError("This identical meter CSV and window set is already attached; no duplicate energy rows were added")
    workloads = {}
    for window in windows:
        run_id = window.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in workloads:
            raise ValueError("Energy window run_id must be a unique nonempty string")
        matches = [row for row in source_rows if row.get("case_id") == run_id and
                   row.get("experiment") == "energy_workload"]
        if len(matches) != 1 or matches[0].get("status") != "ok":
            raise ValueError("Each energy window must match exactly one successful energy_workload result")
        source = matches[0]
        source_metrics = source.get("metrics", {})
        queries = source_metrics.get("queries") if isinstance(source_metrics, dict) else None
        if type(queries) is not int or queries < 1:
            raise ValueError("Energy workload must record its positive actual query count")
        if not isinstance(source.get("provenance", {}), dict):
            raise ValueError("Energy workload provenance must be an object")
        workloads[run_id] = source
    result = metrics.analyze_energy(args.csv, windows)
    runs = result.get("runs")
    if (result.get("available") is not True or result.get("measurement") != "external_board_input_power" or
            not isinstance(runs, list) or len(runs) != len(windows)):
        raise ValueError("Energy analysis did not produce validated measurements for all windows")
    for measured in runs:
        if (not isinstance(measured, dict) or measured.get("run", {}).get("coverage_fraction") != 1.0 or
                measured.get("idle", {}).get("coverage_fraction") != 1.0):
            raise ValueError("Energy analysis has incomplete run or idle coverage")
        for key in ("total_board_energy_j", "incremental_energy_j", "idle_mean_power_w"):
            value = measured.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Energy analysis returned an invalid numeric measurement")
    if hashlib.sha256(args.csv.read_bytes()).hexdigest() != csv_hash:
        raise ValueError("Meter CSV changed during analysis; no energy rows were added")
    meter_provenance = {
        "measurement": result["measurement"], "integration": result["integration"],
        "meter_csv_sha256": csv_hash, "energy_windows_sha256": windows_hash,
        "complete_run_and_idle_coverage": True,
        "sample_count": result["sample_count"], "input_columns": result["input_columns"],
        "source_experiment": "energy_workload",
        "energy_scope": "Whole-board input energy, not CPU-only energy; one trace measures one board",
        "one_trace_measures_one_board": True,
    }
    rows = []
    for measured in runs:
        source = workloads[measured["run_id"]]
        queries = source["metrics"]["queries"]
        system = source.get("system_info", source.get("platform", manifest.get("system_info", {})))
        rows.append({
            "experiment": "energy", "source_experiment": source["experiment"],
            "case_id": measured["run_id"], "status": "ok",
            "role": source.get("role", manifest.get("role", "unspecified")),
            "platform": source.get("platform", system),
            "data_kind": source.get("data_kind", "unspecified"), "system_info": system,
            "metrics": {**measured, "queries": queries,
                        "total_j_per_query": measured["total_board_energy_j"] / queries,
                        "incremental_j_per_query": measured["incremental_energy_j"] / queries},
            "provenance": {**source.get("provenance", {}), **meter_provenance},
        })
    encoded = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                      for row in rows)
    metrics.atomic_json(run_dir / "energy_analysis.json", result)
    with results_path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    print(report.analyze(run_dir))
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 2
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor()
        if args.command == "token":
            from .service import create_token
            create_token(args.out)
            print(args.out)
        elif args.command == "serve":
            return _serve(args)
        elif args.command == "dataset":
            from . import data
            if args.dataset_kind == "synthetic":
                result = data.create_synthetic(args.out, args.epochs, args.blocks, args.block_bytes, args.seed)
            else:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.rpc_env):
                    raise ValueError("--rpc-env requires an environment variable name, not an endpoint URL")
                rpc_url = os.environ.get(args.rpc_env)
                if not rpc_url:
                    raise ValueError(f"Set the {args.rpc_env} environment variable to an Ethereum JSON-RPC endpoint")
                result = data.fetch_ethereum(args.out, rpc_url, args.count, args.start, args.timeout)
            print(result)
        elif args.command == "run":
            if bool(args.provider) != bool(args.token_file):
                raise ValueError("--provider and --token-file must be supplied together")
            from .runner import run_suite
            result = Path(run_suite(vars(args)))
            print(result)
            manifest = json.loads((result / "run_manifest.json").read_bytes())
            if not isinstance(manifest, dict):
                raise ValueError("Run manifest must contain an object")
            failed = manifest.get("failed_records", 0)
            if type(failed) is not int or failed < 0:
                raise ValueError("Run manifest failed_records must be a nonnegative integer")
            if failed or manifest.get("fatal_error"):
                print(f"Run contains {failed} failed record(s); inspect its report and logs.", file=sys.stderr)
                return 1
        elif args.command == "report":
            from .report import analyze
            print(analyze(args.run_dir))
        elif args.command == "compare":
            from .runner import combine_runs
            print(combine_runs(args.run_dirs, args.out))
        elif args.command == "energy":
            return _energy(args)
        elif args.command == "test":
            suite = unittest.defaultTestLoader.discover(str(SUITE_ROOT / "tests"), pattern="test_*.py")
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
