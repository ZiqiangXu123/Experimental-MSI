from __future__ import annotations
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

workspace = Path(__file__).resolve().parents[3]
revision = workspace / 'Technical_Consistency_2026-11-17'
logs = revision / 'working/e7'
python = '<PYTHON_EXECUTABLE>'
tests = revision / 'code_revised/experiment/experiments/e7_integrity_availability/tests'
env = dict(os.environ)
env['DYLD_LIBRARY_PATH'] = str(logs / 'lib')
results = []
for version, pattern, log_name in [('code_original', 'test_checkpoint_policy.py', 'checkpoint_before.log'),
                                    ('code_revised', 'test_checkpoint_policy.py', 'checkpoint_after.log'),
                                    ('code_revised', 'test_*.py', 'all_tests.log')]:
    src = revision / version / 'experiment/experiments/e7_integrity_availability/src'
    env['PYTHONPATH'] = str(src)
    command = [python, '-m', 'unittest', 'discover', '-s', str(tests), '-p', pattern, '-v']
    result = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True)
    text = result.stdout + result.stderr
    (logs / log_name).write_text(text)
    match = re.search(r'Ran (\d+) tests?', text)
    failures = re.findall(r'^FAIL: (.*)', text, re.MULTILINE)
    errors = re.findall(r'^ERROR: (.*)', text, re.MULTILINE)
    results.append({'source_version': version, 'test_pattern': pattern, 'command': command,
                    'environment': {'PYTHONPATH': str(src), 'DYLD_LIBRARY_PATH': env['DYLD_LIBRARY_PATH']},
                    'exit_code': result.returncode, 'tests_run': int(match[1]) if match else None,
                    'failures': failures, 'errors': errors, 'log': log_name,
                    'log_sha256': hashlib.sha256(text.encode()).hexdigest()})
receipt = {'created_at': datetime.datetime.now().astimezone().isoformat(),
           'scope': 'targeted regression and existing unit/tiny smoke tests only; no publication-scale rerun',
           'base_public_commit': '4789284b0c40d524c397a3093ca5747e932cb36a',
           'crypto': 'Real Ed25519 through bundled OpenSSL 3.6.2; only library search path changed',
           'results': results}
(logs / 'validation_results.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps([{'source': r['source_version'], 'pattern': r['test_pattern'],
                  'tests': r['tests_run'], 'exit_code': r['exit_code'],
                  'failures': len(r['failures']), 'errors': len(r['errors'])} for r in results], indent=2))
if results[0]['exit_code'] != 1 or len(results[0]['failures']) != 3 or any(r['exit_code'] for r in results[1:]):
    sys.exit('Unexpected regression outcome; inspect raw logs')
