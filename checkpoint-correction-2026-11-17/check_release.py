"""Rebuild and validate this standalone checkpoint correction package."""
from __future__ import annotations

import ctypes
import ctypes.util
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import zipapp
import zipfile


def main() -> int:
    root = Path(__file__).resolve().parent
    checks = root / "release_checks"
    checks.mkdir(exist_ok=True)
    library_name = ctypes.util.find_library("crypto")
    library = ctypes.CDLL(library_name or "libcrypto.so")
    library.OpenSSL_version.argtypes = [ctypes.c_int]
    library.OpenSSL_version.restype = ctypes.c_char_p
    openssl_version = library.OpenSSL_version(0).decode()
    results = []
    bundles = [
        ("e5_anchor_pruning", "exp05_anchor.pyz", 20),
        ("e7_integrity_availability", "exp07_security_availability.pyz", 19),
    ]
    for name, filename, expected_tests in bundles:
        project = root / "experiment" / "experiments" / name
        source = project / "src"
        executable = project / filename
        zipapp.create_archive(
            source, executable, interpreter="/usr/bin/env python3", compressed=True,
            filter=lambda path: "__pycache__" not in path.parts and path.suffix != ".pyc",
        )
        with zipfile.ZipFile(executable) as archive:
            source_names = {p.relative_to(source).as_posix() for p in source.rglob("*")
                            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
            archive_names = {i.filename for i in archive.infolist() if not i.is_dir()}
            if archive_names != source_names:
                raise RuntimeError(f"Zipapp file set differs for {name}")
            for path in source_names:
                if archive.read(path) != (source / path).read_bytes():
                    raise RuntimeError(f"Zipapp differs from source: {name}/{path}")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        tests = [sys.executable, "-m", "unittest", "discover", "-s", str(project / "tests"),
                 "-p", "test_*.py", "-v"]
        run = subprocess.run(tests, cwd=root, env=environment, capture_output=True, text=True)
        log = run.stdout + run.stderr
        public_log = log.replace(str(root), "<PACKAGE_ROOT>")
        test_log = checks / f"{name}.tests.log"
        test_log.write_text(public_log)
        match = re.search(r"Ran (\d+) tests?", log)
        count = int(match[1]) if match else None
        help_run = subprocess.run([sys.executable, str(executable), "--help"], cwd=root,
                                  env=environment, capture_output=True, text=True)
        help_log = checks / f"{name}.help.log"
        help_log.write_text((help_run.stdout + help_run.stderr).replace(str(root), "<PACKAGE_ROOT>"))
        result = {
            "experiment": name,
            "command": f"PYTHONPATH=experiment/experiments/{name}/src python3 -m unittest discover -s experiment/experiments/{name}/tests -p 'test_*.py' -v",
            "tests_run": count, "expected_tests": expected_tests,
            "test_exit_code": run.returncode, "help_exit_code": help_run.returncode,
            "zipapp_source_bytes_match": True,
            "zipapp": executable.relative_to(root).as_posix(),
            "zipapp_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "test_log": test_log.relative_to(root).as_posix(),
            "test_log_sha256": hashlib.sha256(test_log.read_bytes()).hexdigest(),
        }
        results.append(result)
    receipt = {
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Fresh packaging verification and 20+19 correctness tests; no original benchmark or full integrity corpus rerun",
        "base_public_commit": "4789284b0c40d524c397a3093ca5747e932cb36a",
        "python": platform.python_version(), "system": platform.system(),
        "machine": platform.machine(), "openssl": openssl_version,
        "crypto_mocked": False, "results": results,
    }
    (checks / "validation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    success = all(r["test_exit_code"] == 0 and r["help_exit_code"] == 0 and
                  r["tests_run"] == r["expected_tests"] for r in results)
    print(json.dumps({"passed": success, "results": results}, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
