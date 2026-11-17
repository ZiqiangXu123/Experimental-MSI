import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    failures = []
    lines = (root / 'SHA256SUMS.txt').read_text().splitlines()
    for line in lines:
        expected, relative = line.split('  ', 1)
        path = root / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(relative)
    pending = json.loads((root / 'provenance/pending_inputs.json').read_text())
    downloads = [entry for entry in pending if entry.get('availability') == 'release_assets']
    missing = [entry['path'] for entry in pending
               if entry.get('availability') != 'release_assets'
               and not (root / entry['path']).exists()]
    print(json.dumps({
        'checked_files': len(lines),
        'hash_failures': failures,
        'missing_local_replay_inputs': missing,
        'original_runs_download_separately': downloads,
        'checksum_scope': json.loads((root / 'provenance/checksum_scope.json').read_text()),
        'full_workload_rerun': False,
    }, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
