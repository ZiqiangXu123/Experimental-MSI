import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.out.resolve()
    if destination.exists():
        parser.error('Output directory already exists; choose a new directory.')
    source = root / 'implementation'
    if destination == source or source in destination.parents:
        parser.error('Output must be outside implementation/.')
    destination.mkdir(parents=True)
    names = {'msi_supplement', 'tests', 'vendor', 'configs', 'scripts', 'run.sh', 'hpc_ltu.sbatch', 'hpc_nfs_migration_check.sbatch'}
    for item in source.iterdir():
        if item.name not in names:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'outputs', 'provider_data', 'secrets', 'hpc_job_logs'))
        else:
            shutil.copy2(item, target)
    (destination / 'tests/test_setup_timeout.py').unlink()
    overlay = root / 'source_variants/hpc_1_0_0'
    for file in overlay.rglob('*'):
        if file.is_file():
            target = destination / file.relative_to(overlay)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    print(destination)


if __name__ == '__main__':
    main()
