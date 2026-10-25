"""Durable two-GPU execution: smoke, symmetric selection, freeze, held-out test."""
import argparse
import subprocess
import time
import numpy as np
from symmetric_common import *

PYTHON=ROOT/'.venv/bin/python'


def run(stage,phase='tuning',shards=(0,1),limit=0,fixtures=False):
    started=time.time()
    programs=[]
    for shard in shards:
        program=f'symmetric-q2-gpu{shard}'
        command=f'{PYTHON} -u {ROOT}/scripts/run_symmetric_selection.py --stage {stage} --phase {phase} --shard {shard}'
        if limit:command+=f' --limit {limit}'
        if fixtures:command+=' --fixtures'
        config=f'''[program:{program}]
directory={ROOT}
command={command}
environment=CUDA_VISIBLE_DEVICES="{shard}",HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",PYTHONUNBUFFERED="1",OMP_NUM_THREADS="8",TOKENIZERS_PARALLELISM="false"
autostart=false
autorestart=false
stdout_logfile={OUT}/worker{shard}.log
redirect_stderr=true
stdout_logfile_maxbytes=0
'''
        Path(f'/etc/supervisor/conf.d/{program}.conf').write_text(config)
        programs.append(program)
    subprocess.run(['supervisorctl','reread'],check=True)
    subprocess.run(['supervisorctl','update'],check=True)
    subprocess.run(['supervisorctl','start',*programs],check=True)
    manifest=read(OUT/'manifest.json');manifest.update(status=f'{stage}_{phase}',active_stage={'stage':stage,'phase':phase,'shards':list(shards),'limit':limit,'fixtures':fixtures,'started_unix':started})
    save(OUT/'manifest.json',manifest)
    while True:
        receipts=[]
        for shard in shards:
            matches=[read(p) for p in (OUT/'runtime').glob(f'{stage}.{phase}.shard{shard}.*.json')]
            matches=[r for r in matches if r['started_unix']>=started]
            if matches:
                receipt=max(matches,key=lambda r:r['started_unix'])
                assert receipt['status']=='complete',receipt.get('error','worker failed')
                receipts.append(receipt)
        done=len(receipts)==len(shards)
        counts=len(list((OUT/'generations'/phase).glob('*.json')))
        print(f'{stage} {phase}: {len(receipts)}/{len(shards)} shards complete; {counts} saved generation batches; elapsed {time.time()-started:.0f}s',flush=True)
        if done:return receipts
        state=subprocess.run(['supervisorctl','status',*programs],text=True,capture_output=True).stdout
        if all(not any(program in line and ('RUNNING' in line or 'STARTING' in line) for line in state.splitlines()) for program in programs):
            # Read receipts again after process exit to avoid a polling-boundary race.
            time.sleep(1)
            latest=[read(p) for p in (OUT/'runtime').glob(f'{stage}.{phase}.shard*.*.json')]
            latest=[r for r in latest if r['started_unix']>=started and r['shard'] in shards]
            if len(latest)==len(shards) and all(r['status']=='complete' for r in latest):return latest
            raise RuntimeError(f'Workers exited before successful receipts. Inspect {OUT}/worker*.log')
        time.sleep(20)


def smoke():
    if (OUT/'smoke_verified.json').exists():
        receipt=read(OUT/'smoke_verified.json')
        for name,digest in receipt['sha256'].items():assert sha(ROOT/name)==digest
        print('Smoke already verified; resuming full study',flush=True);return
    started=time.time()
    shared=run('shared',shards=(0,))
    construction=[]
    # Build the shared canonical negative mean once, then let the other shard reuse it.
    for shard in [0,1]:construction+=run('construct',shards=(shard,),fixtures=True)
    nla=run('nla',fixtures=True)
    generation=run('smoke',fixtures=True)
    fixture_ids=read(OUT/'smoke_fixtures.json')['concept_ids'];paths=[]
    assert not (set(fixture_ids) & set(read(OUT/'pool.json')['concept_ids']))
    for cid in fixture_ids:
        path=OUT/'smoke'/f'c{cid}.p0.json';row=read(path)
        assert row['smoke'] and list(row['generation_arms'])==expected_arms('tuning')
        assert all(len(v)==2 for v in row['generation_arms'].values())
        assert row['batch_integrity_sha256']==signature({k:v for k,v in row.items() if k!='batch_integrity_sha256'})
        paths.append(path)
    # A direct rerun must verify/skip smoke outputs and preserve their exact hashes.
    before={str(p.relative_to(ROOT)):sha(p) for p in paths}
    run('smoke',fixtures=True)
    assert before=={str(p.relative_to(ROOT)):sha(p) for p in paths}
    gen_seconds=float(np.mean([read(p)['elapsed_seconds'] for p in paths]))
    construction_seconds=float(np.mean([r['elapsed_seconds'] for r in construction]))
    nla_seconds=float(np.mean([r['elapsed_seconds'] for r in nla]))/4
    # 50 concepts per GPU, 400 tuning and 800 screening batches; max 40 unique test concepts.
    estimate=(100*construction_seconds+1520*nla_seconds+400*gen_seconds+1120*gen_seconds*.65)/2
    estimate+=shared[0]['elapsed_seconds']*4
    receipt={'status':'passed','protocol_sha256':sha(OUT/'protocol.json'),'sha256':before,'elapsed_seconds':time.time()-started,
             'responses':72,'batch_resume_verified':True,'smoke_outcomes_excluded_from_study':True,
             'measured_tuning_batch_seconds':gen_seconds,'projected_wall_hours':[estimate/3600*.8,estimate/3600*1.6],
             'eta_basis':'Measured two-concept smoke; 100 construction, 1520 prompt-specific direction preparations maximum, 400 tuning and 1120 screening/evaluation target batches; excludes analyst implementation time'}
    save(OUT/'smoke_verified.json',receipt)
    print('SMOKE PASSED; estimated full-run hours: '+str(receipt['projected_wall_hours']),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-only',action='store_true');args=parser.parse_args()
    verify_freeze()
    lock_path=CACHE/'coordinator.lock';lock_path.parent.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=lock_path.open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if read(OUT/'manifest.json')['status']=='complete':
        print('Study complete; rerun analyze_symmetric_selection.py to reproduce analysis',flush=True);return
    smoke()
    if args.smoke_only:return
    run('construct')
    for phase in ['tuning','screen']:
        if phase!='tuning':run('shared',phase,shards=(0,))
        run('nla',phase);run('generate',phase);run('validate',phase)
    subprocess.run([str(PYTHON),'scripts/analyze_symmetric_selection.py','--action','freeze_panels'],cwd=ROOT,check=True)
    manifest=read(OUT/'manifest.json');manifest.update(status='panels_frozen_before_held_out',panels_sha256=sha(OUT/'panels_frozen.json'),held_out_started=True,held_out_started_unix=time.time())
    save(OUT/'manifest.json',manifest)
    run('shared','evaluation',shards=(0,))
    run('nla','evaluation');run('generate','evaluation');run('validate','evaluation')
    subprocess.run([str(PYTHON),'scripts/analyze_symmetric_selection.py','--action','analyze'],cwd=ROOT,check=True)
    print('STUDY COMPLETE',flush=True)

if __name__=='__main__':main()
