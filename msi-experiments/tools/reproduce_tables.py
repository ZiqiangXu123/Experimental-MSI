import argparse
import filecmp
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=Path('work/recomputed'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.out.resolve()
    inputs = root / 'results'
    if output == inputs or inputs in output.parents or output == root / 'tables' or root / 'tables' in output.parents:
        parser.error('Output must be outside the recorded results and reference tables.')
    scripts = {'performance': 'analyze_performance.py', 'platform_audit': 'audit_ethernet2.py', 'storage_migration': 'analyze.py'}
    for category, filename in scripts.items():
        subprocess.run([sys.executable, str(root / 'analysis' / category / filename), '--input-root', str(inputs), '--out', str(output / category)], check=True)
    reference = sorted((root / 'tables').rglob('*'))
    differences = [str(file.relative_to(root / 'tables')) for file in reference if file.is_file() and not filecmp.cmp(file, output / file.relative_to(root / 'tables'), shallow=False)]
    print(json.dumps({'reference_files': sum(file.is_file() for file in reference), 'mismatches': differences}, indent=2))
    if differences:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
