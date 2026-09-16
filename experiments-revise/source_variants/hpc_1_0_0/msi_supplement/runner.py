
from __future__ import annotations
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from . import __version__, core, data, metrics
from .service import Client, read_token

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    'smoke': {'sizes': [16, 64], 'queries': 12, 'warmup': 2, 'trials': 1, 'pool': 4, 'memory_limit_mib': 128, 'pressure_mib': 16, 'idle_seconds': 0},
    'paper': {'sizes': [512, 4096], 'queries': 200, 'warmup': 20, 'trials': 5, 'pool': 16, 'memory_limit_mib': 256, 'pressure_mib': 64, 'idle_seconds': 3},
}


def _new_dir(path):
    path = Path(path).resolve()
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f'Output must be new or empty: {path}')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append(out, record):
    with (out / 'results.jsonl').open('a') as f:
        f.write(json.dumps(record, sort_keys=True, allow_nan=False) + '\n')
        f.flush()
    print(f"[{record['status']}] {record['experiment']} {record['case_id']}", flush=True)


def _child(out, cfg):
    raw = out / 'raw'
    raw.mkdir(exist_ok=True)
    inp, result = raw / (cfg['case_id'] + '.input.json'), raw / (cfg['case_id'] + '.json')
    metrics.atomic_json(inp, cfg)
    with (raw / (cfg['case_id'] + '.log')).open('w') as log:
        proc = subprocess.run([sys.executable, '-m', 'msi_supplement.worker', str(inp), str(result)],
                              cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    value = json.loads(result.read_text()) if result.exists() else {'error': 'WorkerExit', 'message': f'exit {proc.returncode}; see log'}
    if proc.returncode and 'error' not in value:
        value['error'] = f'WorkerExit{proc.returncode}'
    return value


def _provider_summary(samples):
    values = [s.get('provider_total_ns', 0)/1e6 for s in samples]
    stats = metrics.summarise(values)
    resident = [s['rss_bytes'] for s in samples if isinstance(s.get('rss_bytes'),int) and s['rss_bytes']>0]
    return {'queries': len(samples), 'median_ms': stats['median'], 'p95_ms': stats['p95'],
        'provider_cpu_ms_per_query': sum(s.get('provider_cpu_ns', 0) for s in samples)/max(1,len(samples))/1e6,
        'payload_read_bytes_per_query': sum(s.get('payload_read_bytes', 0) for s in samples)/max(1,len(samples)),
        'support_read_bytes_per_query': sum(s.get('support_read_bytes', 0) for s in samples)/max(1,len(samples)),
        'witness_generate_median_ms': metrics.summarise([s.get('witness_generate_ns',0)/1e6 for s in samples])['median'],
        'rss_bytes': max(resident) if resident else None}


def _integrity(descriptor, response):
    results = {'honest': core.verify_response(descriptor,response)}
    wrong = copy.deepcopy(response)
    payload = bytearray(base64.b64decode(wrong['block_b64']))
    payload[-1] ^= 1
    wrong['block_b64'] = base64.b64encode(payload).decode()
    results['payload_tamper_rejected'] = not core.verify_response(descriptor,wrong)
    for field in ('root_hex','signature_hex'):
        wrong = copy.deepcopy(response)
        value = wrong['certificate'][field]
        wrong['certificate'][field] = ('0' if value[0] != '0' else '1')+value[1:]
        results[field+'_tamper_rejected'] = not core.verify_response(descriptor,wrong)
    wrong = copy.deepcopy(response)
    wrong['query'][0] += 1
    results['shard_substitution_rejected'] = not core.verify_response(descriptor,wrong)
    wrong = copy.deepcopy(response)
    wrong['query'][1] += 1
    results['epoch_substitution_rejected'] = not core.verify_response(descriptor,wrong)
    wrong = copy.deepcopy(response)
    wrong['mode'] = 'archive' if descriptor['mode'] != 'archive' else 'full'
    results['mode_substitution_rejected'] = not core.verify_response(descriptor,wrong)
    return results


def run_suite(opts: dict) -> Path:
    profile = opts.get('profile','smoke')
    cfg = PROFILES[profile].copy()
    for key in ('sizes','queries','trials'):
        if opts.get(key) is not None:
            cfg[key]=opts[key]
    if not cfg['sizes'] or any(type(n) is not int or not 1 <= n <= 16384 for n in cfg['sizes']):
        raise ValueError('Each epoch size must be in 1..16384')
    if not 1 <= cfg['trials'] <= 30 or not 1 <= cfg['queries'] <= 100000:
        raise ValueError('Trials must be 1..30 and queries 1..100000')
    out = _new_dir(opts['out'])
    platform = metrics.system_info()
    platform['result_storage']=metrics.storage_for_path(out)
    seed = int(opts.get('seed',2026))
    provider_url = opts.get('provider')
    if provider_url and not opts.get('token_file'):
        raise ValueError('--provider requires --token-file')
    token_path = str(Path(opts['token_file']).resolve()) if opts.get('token_file') else None
    client = Client(provider_url, read_token(Path(token_path))) if provider_url else None
    provider_platform = client.call('/health', {})['platform'] if client else platform
    manifest = {'suite_version': __version__, 'profile': profile, 'role': opts['role'], 'config': cfg,
        'seed': seed, 'platform': platform, 'system_info': platform,
        'provider_platform': provider_platform if client else None,
        'started_unix_ns': time.time_ns(), 'native_finality_verified': False,
        'publication_ready': False, 'limitations': ['Experiment-generated Ed25519 trust anchor',
            'No native sharded consensus or committee rotation', 'No physical flash power-cut test',
            'External energy trace and real dataset are optional inputs; missing evidence stays explicit']}
    manifest['code_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                            for directory in (ROOT/'msi_supplement',ROOT/'vendor'/'experiment')
                            for p in sorted(directory.rglob('*.py')) if '__pycache__' not in p.parts}
    metrics.atomic_json(out/'run_manifest.json',manifest)
    records = []
    windows = []
    def record(experiment, case_id, data_kind='synthetic', status='ok', values=None, provenance=None, role=None, host=None):
        item = {'experiment':experiment, 'case_id':case_id, 'role':role or opts['role'],
                'platform':host or platform, 'data_kind':data_kind, 'status':status,
                'metrics':values or {}, 'provenance':provenance or {}}
        records.append(item)
        _append(out,item)
        return item
    try:
        dataset_path = out/'datasets'/'synthetic'
        data.create_synthetic(dataset_path, 1, max(cfg['sizes']), 2048, seed)
        synthetic_manifest, synthetic_payloads = data.load_dataset(dataset_path)
        workloads = [('synthetic',n,synthetic_payloads[:n],synthetic_manifest) for n in cfg['sizes']]
        if opts.get('dataset'):
            dm, dp = data.load_dataset(Path(opts['dataset']))
            
            selected, total = [],0
            for payload in dp:
                if len(selected) >= max(cfg['sizes']) or total+len(payload)>32*1048576:
                    break
                selected.append(payload); total += len(payload)
            if not selected:
                raise ValueError('No supplied block fits the 32 MiB case bound')
            workloads.append((dm['data_kind'],len(selected),selected,dm))
            metrics.atomic_json(out/'supplied_dataset_manifest.json',dm)
        else:
            record('real_dataset','no-supplied-real-dataset',status='skipped',provenance={'reason':'Pass --dataset after Ethereum RPC export; synthetic data are never relabelled.'})
        mode_jobs = [(kind,n,payloads,dm,trial,mode) for kind,n,payloads,dm in workloads for trial in range(cfg['trials']) for mode in core.MODES]
        random.Random(seed).shuffle(mode_jobs)
        metrics.atomic_json(out/'execution_order.json', [{'data_kind':kind,'n':n,'trial':trial,'mode':mode} for kind,n,_,_,trial,mode in mode_jobs])
        for job_index,(kind,n,payloads,dm,trial,mode) in enumerate(mode_jobs):
            base = f'{job_index:04d}-{kind}-n{n}-{mode}-t{trial}'
            print(f'Preparing {base}',flush=True)
            case = out/'cases'/base
            pool = out/'proof_fixtures'/base
            pool.mkdir(parents=True)
            remote = bool(client and mode != 'archive')
            if remote:
                created = client.call('/create',{'mode':mode,'payloads_b64':[base64.b64encode(p).decode() for p in payloads]})
                descriptor, account = created['descriptor'], created['accounting']
                case.mkdir(parents=True)
                descriptor_path = case/'descriptor.json'
                
                descriptor_path.write_bytes(core._json_bytes(descriptor))
                account['verifier_allocated_bytes']=descriptor_path.stat().st_blocks*512
                account['physical_case_bytes_on_provider_device']=account['total_materialized_bytes']
                account['physical_descriptor_bytes_on_verifier_device']=descriptor_path.stat().st_size
                account['provider_setup_descriptor_replica_bytes']=descriptor_path.stat().st_size
                account['physical_deployed_case_total_bytes']=account['total_materialized_bytes']+descriptor_path.stat().st_size
                remote_case_id = created['case_id']
            else:
                descriptor = core.create_case(payloads,case,mode)
                account = core.storage_accounting(case)
                descriptor_path = case/'verifier'/'descriptor.json'
                remote_case_id = None
            positions = random.Random(seed+trial*1009+n).sample(range(1,n+1),min(cfg['pool'],n))
            prov = {'mode':mode,'n':n,'trial':trial,'profile':profile,
                'dataset_sha256': dm['blocks_sha256'], 'selected_payload_sha256':hashlib.sha256(b''.join(payloads)).hexdigest(),
                'dataset_source':dm.get('provenance',{'generator':'deterministic synthetic'}),'chain_id':dm.get('chain_id'),
                'selected_count':n,'selection':'contiguous prefix',
                'transport':'tcp' if remote else 'local','wire_format':'http1-json-base64' if remote else 'json proof fixture',
                'client_machine_id':platform.get('machine_id'), 'server_machine_id':provider_platform.get('machine_id') if remote else platform.get('machine_id'),
                'network_kind':opts.get('network_kind','unspecified'),
                'native_finality_verified':False,'trust_anchor':'experiment-generated Ed25519 descriptor provisioned at setup',
                'cache_state':'OS page cache uncontrolled; warm query repetition; no cold-flash claim',
                'provider_case_id':remote_case_id,'storage_scope':'logical role ownership; experiment fixtures and setup reported separately'}
            record('storage',base+'-storage',kind,values=account,provenance=prov)
            provider_samples=[]
            for position in positions:
                package = client.call('/query',{'case_id':remote_case_id,'position':position}) if remote else core.query_store(case,position)
                provider_samples.append(package['provider'])
                metrics.atomic_json(pool/f'{position}.json',package)
            record('provider',base+'-provider',kind,values=_provider_summary(provider_samples),provenance={**prov,'source':'proof_fixture_preparation','timing_scope':'payload/support read and proof construction; JSON serialization and network excluded','sample_count':len(positions)},role='provider',host=provider_platform if remote else platform)
            common = {'kind':'measure','descriptor':str(descriptor_path),'pool':str(pool),'positions':positions,
                'queries':cfg['queries'],'warmup':cfg['warmup'],'case_path':str(case),'remote_case_id':remote_case_id,
                'provider':provider_url if remote else None,'token_file':token_path if remote else None,
                'idle_seconds':cfg['idle_seconds']}
            tasks = [('local','pool',0,0)]
            if mode != 'archive':
                tasks.append(('memory_pressure','pool',cfg['memory_limit_mib'],cfg['pressure_mib']))
            if remote:
                tasks.append(('network','network',0,0))
            elif mode == 'archive':
                tasks.append(('archive_retrieval','archive_disk',0,0))
            if profile == 'paper' and kind == 'synthetic' and n == cfg['sizes'][0] and trial == 0:
                tasks.append(('energy_workload','network' if remote else ('archive_disk' if mode=='archive' else 'pool'),0,0))
            for experiment,source,limit,pressure in tasks:
                cid = base+'-'+experiment
                worker_cfg={**common,'case_id':cid,'source':source,'memory_limit_mib':limit,'pressure_mib':pressure}
                worker_cfg['min_seconds']=10 if experiment=='energy_workload' else 0
                
                if source != 'network':
                    worker_cfg['provider']=None
                measured=_child(out,worker_cfg)
                p={**prov,'implementation':'persistent_adapter_original_crypto','source':source,
                   'memory_limit_mib':limit,'pressure_mib':pressure,
                   'timing_scope':'pre-delivered response acceptance only' if source=='pool' else 'request/read through verified acceptance',
                   'cpu_scope':'verifier acceptance CPU only','memory_scope':'fresh worker, one live response, optional pressure buffer; fixtures excluded',
                   'memory_limit_kind':'RLIMIT_AS virtual address space' if limit else 'none'}
                if 'error' in measured:
                    record(experiment,cid,kind,'failed',provenance={**p,**measured})
                else:
                    record(experiment,cid,kind,values=measured['metrics'],provenance=p)
                    if experiment=='network':
                        record('provider',cid+'-provider',kind,values=_provider_summary(measured['provider_samples']),provenance={**prov,'source':'warm_network_queries','timing_scope':'provider processing for measured network queries; JSON serialization and network excluded'},role='provider',host=provider_platform)
                    if experiment=='energy_workload':
                        windows.append(measured['window'])
            honest = json.loads((pool/f'{positions[0]}.json').read_text())['response']
            checks = _integrity(descriptor,honest)
            record('integrity',base+'-integrity',kind,'ok' if all(checks.values()) else 'failed',values={**checks,'cases':len(checks),'failures':sum(not x for x in checks.values())},provenance={**prov,'scope':'finite adversarial regression cases, not a cryptographic security bound'})
            if remote:
                rejected=False
                start=time.perf_counter_ns()
                try:
                    client.call('/query',{'case_id':remote_case_id,'position':positions[0],'fault':'disconnect'})
                except (OSError,RuntimeError,ValueError):
                    rejected=True
                interrupted_ns=time.perf_counter_ns()-start
                recovered=client.call('/query',{'case_id':remote_case_id,'position':positions[0]})['response']
                ok=recovered.get('query')==[descriptor['shard'],descriptor['epoch'],positions[0]] and core.verify_response(descriptor,recovered)
                record('disconnect',base+'-disconnect',kind,'ok' if rejected and ok else 'failed',values={'disconnect_cases':1,'retrieval_failed_safely':rejected,'recovered':ok,'false_accepts':0 if rejected else 1,'failure_detection_ms':interrupted_ns/1e6},provenance={**prov,'fault':'server closes actual TCP query connection; next request reconnects','not_tested':'RF loss, board outage, physical link disconnection'})
        if not opts.get('skip_original'):
            for n in cfg['sizes']:
                for trial in range(cfg['trials']):
                    schemes=['B1_verified','B2_full','B3_leaf','B4_ext']
                    random.Random(seed+n+trial).shuffle(schemes)
                    for scheme in schemes:
                        cid=f'original-n{n}-{scheme}-t{trial}'
                        result=_child(out,{'kind':'original','case_id':cid,'n':n,'scheme':scheme,'queries':cfg['queries'],'warmup':cfg['warmup'],'seed':seed+trial})
                        p={'implementation':'original_e2','scheme':scheme,'n':n,'trial':trial,'profile':profile,'workload_seed':seed,'queries':cfg['queries'],'warmup':cfg['warmup'],'transport':'local','timing_scope':'unchanged original E2 acceptance after in-process handoff','memory_scope':'original full fixture included; not deployment verifier RSS'}
                        if 'error' in result:
                            record('original_e2',cid,status='failed',provenance={**p,**result})
                        else:
                            original=result['original_result']; timing=original['timing']; stats=timing['acceptance_ns']
                            record('original_e2',cid,values={'queries':cfg['queries'],'median_ms':stats['median']/1e6,'p95_ms':stats['p95']/1e6,'p99_ms':stats['p99']/1e6},provenance=p)
        if not opts.get('skip_migration'):
            from .migration import run_migration_suite
            for item in run_migration_suite(out/'migration',profile=profile,blocks=16 if profile=='smoke' else 512,block_bytes=2048,repeats=1 if profile=='smoke' else 5):
                item['role']=opts['role']; item['platform']={**platform,**item.get('platform',{})}
                item.setdefault('provenance',{})['profile']=profile
                records.append(item); _append(out,item)
        else:
            record('migration','migration-skipped',status='skipped',provenance={'reason':'--skip-migration'})
        if not client:
            record('network','no-remote-provider',status='skipped',provenance={'reason':'Pass --provider and --token-file for actual two-device TCP measurements'})
        record('energy','no-external-meter-csv',status='skipped',provenance={'reason':'Import synchronized external power CSV with energy command; CPU time is not energy'})
        record('native_finality','native-chain-integration-not-implemented',status='skipped',provenance={'reason':'RPC finalized block replay does not verify native consensus or sharded cross-event consistency'})
        manifest['completed_unix_ns']=time.time_ns()
        manifest['failed_records']=sum(r['status']=='failed' for r in records)
    except Exception as exc:
        manifest['fatal_error']={'type':type(exc).__name__,'message':str(exc)}
        record('suite','fatal-error',status='failed',provenance=manifest['fatal_error'])
        raise
    finally:
        if client: client.close()
        metrics.atomic_json(out/'run_manifest.json',manifest)
        metrics.atomic_json(out/'energy_windows.json',windows)
        from .report import analyze
        if (out/'results.jsonl').exists(): analyze(out)
    return out


def combine_runs(run_dirs: list[Path], out: Path) -> Path:
    out=_new_dir(out)
    sources=[]
    for index,source in enumerate(run_dirs):
        source=Path(source).resolve()
        manifest=json.loads((source/'run_manifest.json').read_text())
        sources.append({'path':str(source),'manifest':manifest,'results_sha256':hashlib.sha256((source/'results.jsonl').read_bytes()).hexdigest()})
        for line in (source/'results.jsonl').read_text().splitlines():
            if not line.strip(): continue
            item=json.loads(line)
            item['case_id']=f'source{index}-'+item['case_id']
            item.setdefault('provenance',{})['source_run']=str(source)
            item['provenance']['profile']=manifest.get('profile','unknown')
            _append(out,item)
    profile='paper' if all(s['manifest'].get('profile')=='paper' for s in sources) else 'smoke'
    metrics.atomic_json(out/'run_manifest.json',{'suite_version':__version__,'profile':profile,'combined':True,'sources':sources,'native_finality_verified':False})
    from .report import analyze
    return analyze(out)
