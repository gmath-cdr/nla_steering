"""GPU-local resumable workers for the prospective symmetric Q2 experiment."""
import argparse
import fcntl
import gc
import os
import time
import traceback
from pathlib import Path
from symmetric_common import *
import torch
from experiments import steering, vectors
from experiments.discovery500 import capture_batch, append_edit
from experiments.diffmean_discovery500 import batch_token_activations
from experiments.run_panel_baselines import axbench_trim, chat
from nla_inference import NLAStandalone, NLACritic


def tensor_save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    torch.save(value,temp);temp.replace(path)


def unload():
    gc.collect();torch.cuda.empty_cache()


def cache_vector(path,receipt,build):
    meta=path.with_suffix('.receipt.json')
    if path.exists() and meta.exists():
        existing=read(meta)
        assert existing['inputs']==receipt and existing['sha256']==sha(path)
        value=torch.load(path,weights_only=True,map_location='cpu')
    else:
        value=build().float().cpu()
        tensor_save(path,value)
        save(meta,{'inputs':receipt,'sha256':sha(path)})
    assert torch.isfinite(value).all()
    return value


def shared(phase):
    protocol,pool,prompts,terms=load_inputs()
    if phase=='evaluation': assert (OUT/'panels_frozen.json').exists()
    indices=phase_indices(phase)
    signature_protocol=sha(OUT/'protocol.json')
    complete=OUT/'shared'/f'{phase}.complete.json'
    if complete.exists():
        for name,digest in read(complete)['sha256'].items():assert sha(ROOT/name)==digest
        print(f'shared {phase}: verified resume',flush=True);return
    target,tokenizer=steering.load_target(str(ROOT/protocol['models']['target']['path']),'cuda:0')
    for pi in indices:
        cache_vector(CACHE/'activations'/f'p{pi}.pt',{'protocol':signature_protocol,'prompt':prompts[pi]['text']},lambda pi=pi:capture_batch(target,tokenizer,[prompts[pi]['text']],20)[0])
    del target,tokenizer;unload()
    actor=NLAStandalone(ROOT/protocol['models']['av']['path'],'cuda:0')
    for pi in indices:
        path=OUT/'shared'/f'p{pi}.explanations.json'
        rows=read(path) if path.exists() else []
        for ri,row in enumerate(rows):
            assert row['seed']==20261018+pi*10+ri+1
        for ri in range(len(rows),3):
            rng=20261018+pi*10+ri+1
            torch.manual_seed(rng);torch.cuda.manual_seed_all(rng)
            raw=actor.generate(torch.load(CACHE/'activations'/f'p{pi}.pt',weights_only=True),extract_explanation=False,temperature=1.,max_new_tokens=200)
            original=raw if '<explanation>' in raw else f'<explanation>{raw}</explanation>'
            rows.append({'rollout':ri,'seed':rng,'original_explanation':original,'prompt':prompts[pi]['text'],'protocol_sha256':signature_protocol})
            save(path,rows)
            print(f'AV {phase} prompt {pi} rollout {ri}',flush=True)
    del actor;unload()
    critic=NLACritic(ROOT/protocol['models']['ar']['path'],device='cuda:0')
    for pi in indices:
        for ri,row in enumerate(read(OUT/'shared'/f'p{pi}.explanations.json')):
            cache_vector(CACHE/'original_reconstructions'/f'p{pi}.r{ri}.pt',{'protocol':signature_protocol,'explanation':row['original_explanation']},lambda row=row:critic.reconstruct(row['original_explanation']))
    del critic;unload()
    paths=[OUT/'shared'/f'p{pi}.explanations.json' for pi in indices]
    paths += [CACHE/'activations'/f'p{pi}.pt' for pi in indices]
    paths += [CACHE/'original_reconstructions'/f'p{pi}.r{ri}.pt' for pi in indices for ri in range(3)]
    save(complete,{'phase':phase,'protocol_sha256':signature_protocol,'sha256':{str(p.relative_to(ROOT)):sha(p) for p in paths}})


def construct(ids):
    import pyarrow.parquet as pq
    protocol,pool,prompts,terms=load_inputs()
    target,tokenizer=steering.load_target(str(ROOT/protocol['models']['target']['path']),'cuda:0')
    rows=pq.read_table(ROOT/'src/axbench/axbench/concept500/prod_9b_l20_v1/generate/train_data.parquet').to_pylist()
    module=steering.decoder_layers(target)[20];trim=axbench_trim(tokenizer)
    negatives=[r for r in rows if r['concept_id']==-1 and r['category']=='negative'][:72]
    assert len(negatives)==72
    # One canonical negative cache, built by shard 0 before shard 1 starts construction.
    negpath=CACHE/'construction'/'negative_mean.pt'
    def mean_activation(examples):
        texts=[chat(tokenizer,str(r['input']),str(r['output'])) for r in examples]
        return torch.cat(batch_token_activations(target,tokenizer,texts,module,trim,8)).mean(0)
    negative=cache_vector(negpath,{'protocol':sha(OUT/'protocol.json'),'count':72,'kind':'negative token pool'},lambda:mean_activation(negatives))
    for cid in ids:
        positive=[r for r in rows if r['concept_id']==cid and r['category']=='positive'][:72]
        assert len(positive)==72
        path=CACHE/'diffmean'/f'c{cid}.pt'
        direction=cache_vector(path,{'protocol':sha(OUT/'protocol.json'),'count':72,'cid':cid,'negative_sha256':sha(negpath)},lambda:steering.normalize(mean_activation(positive)-negative))
        assert abs(float(direction.norm())-1)<1e-5
        freeze(OUT/'construction'/f'c{cid}.json',{'concept_id':cid,'positive_count':72,'negative_count':72,'direction_sha256':sha(path),'negative_mean_sha256':sha(negpath),'trim':list(trim)})
        print(f'constructed DiffMean c{cid}',flush=True)
    del target,tokenizer;unload()


def prepare_directions(ids,phase):
    protocol,pool,prompts,terms=load_inputs()
    assert (OUT/'shared'/f'{phase}.complete.json').exists()
    if phase=='evaluation':assert (OUT/'panels_frozen.json').exists()
    critic=NLACritic(ROOT/protocol['models']['ar']['path'],device='cuda:0')
    for cid in ids:
        for pi in phase_indices(phase):
            originals=read(OUT/'shared'/f'p{pi}.explanations.json')
            fixtures=read(OUT/'smoke_fixtures.json')['concepts']
            concept=pool['concepts'].get(str(cid),fixtures.get(str(cid)))
            assert concept is not None
            edited=[append_edit(r['original_explanation'],concept) for r in originals]
            inputs={'protocol':sha(OUT/'protocol.json'),'cid':cid,'pi':pi,'edited_explanations':edited,'original_sha256':sha(OUT/'shared'/f'p{pi}.explanations.json')}
            norms=[]
            def build():
                deltas=[]
                for ri,text in enumerate(edited):
                    original=torch.load(CACHE/'original_reconstructions'/f'p{pi}.r{ri}.pt',weights_only=True)
                    delta=critic.reconstruct(text)-original
                    assert float(delta.norm())>0
                    norms.append(float(delta.norm()));deltas.append(steering.normalize(delta))
                return steering.normalize(sum(deltas,torch.zeros_like(deltas[0])))
            path=CACHE/'nla'/f'c{cid}.p{pi}.pt'
            direction=cache_vector(path,inputs,build)
            assert abs(float(direction.norm())-1)<1e-5
            receipt=OUT/'nla_receipts'/f'c{cid}.p{pi}.json'
            if receipt.exists():
                assert read(receipt)['direction_sha256']==sha(path)
            else:
                save(receipt,{'concept_id':cid,'prompt_index':pi,'direction_sha256':sha(path),'raw_delta_norms':norms,'source_explanations_sha256':inputs['original_sha256'],'edited_explanations':edited,'protocol_sha256':sha(OUT/'protocol.json')})
        print(f'prepared NLA {phase} c{cid}',flush=True)
    del critic;unload()


def arms_for(cid,pi,phase):
    nla=torch.load(CACHE/'nla'/f'c{cid}.p{pi}.pt',weights_only=True,map_location='cpu')
    diff=torch.load(CACHE/'diffmean'/f'c{cid}.pt',weights_only=True,map_location='cpu')
    result={'unsteered':(None,0.)}
    for s in RANDOM:
        direction=vectors.random_like(diff,s*1000000+cid*100+pi)
        result[f'random_{s}']=(direction,.5)
    if phase=='tuning':
        for method,direction in [('nla',nla),('diffmean',diff)]:
            for alpha in GRID:result[f'{method}@{alpha}']=(direction,alpha)
    else:
        selected=read(OUT/'calibration'/f'c{cid}.json')['selected']
        result.update(nla=(nla,selected['nla']['alpha']),diffmean=(diff,selected['diffmean']['alpha']))
        if phase=='evaluation':result['prompting']=(None,0.)
    return result


@torch.inference_mode()
def generate_batch(model,tokenizer,prompt,concept,arms):
    # Same hook, precision, injection positions and sampling distribution for every method.
    prompts=[prompt + ('\n\nPlease make your answer specifically concern: '+concept+'.' if arm=='prompting' else '') for arm in arms for _ in range(2)]
    encoded=[steering.chat_ids(tokenizer,[{'role':'user','content':p}]) for p in prompts]
    width=max(map(len,encoded));pad=tokenizer.eos_token_id
    tokens=torch.full((len(encoded),width),pad,device=model.device,dtype=torch.long)
    mask=torch.zeros_like(tokens)
    for i,ids in enumerate(encoded):tokens[i,-len(ids):]=torch.tensor(ids,device=model.device);mask[i,-len(ids):]=1
    directions=torch.stack([steering.normalize(d) if d is not None else torch.zeros(3584) for d,a in arms.values() for _ in range(2)])
    alphas=torch.tensor([a for d,a in arms.values() for _ in range(2)],device=model.device)
    def hook(_m,_i,output):
        hidden=output[0] if isinstance(output,tuple) else output
        norm=hidden[:,-1].float().norm(dim=-1)
        hidden[:,-1:] += alphas[:,None,None] * norm[:,None,None] * directions.to(hidden.device,hidden.dtype)[:,None,:]
        return output
    handle=steering.decoder_layers(model)[20].register_forward_hook(hook)
    try:
        output=model.generate(tokens,attention_mask=mask,max_new_tokens=96,temperature=.7,top_p=.8,top_k=20,repetition_penalty=1.05,do_sample=True,pad_token_id=pad)
    finally:handle.remove()
    responses=[tokenizer.decode(row[width:],skip_special_tokens=True).strip() for row in output]
    return {arm:responses[2*i:2*i+2] for i,arm in enumerate(arms)}


def generate(ids,phase,smoke=False):
    protocol,pool,prompts,terms=load_inputs()
    actual_phase='tuning' if smoke else phase
    indices=phase_indices(actual_phase)[:1] if smoke else phase_indices(phase)
    if not smoke and phase=='evaluation':
        panel=read(OUT/'panels_frozen.json');assert all(cid in panel['union_ids'] for cid in ids)
    pending=[]
    for cid in ids:
        for pi in indices:
            path=(OUT/'smoke'/f'c{cid}.p{pi}.json') if smoke else task_path(phase,cid,pi)
            if path.exists() and not smoke:
                validate_batch(path,cid,phase,pi,prompts[pi]['text'],sha(OUT/'protocol.json'))
            elif path.exists():
                assert read(path)['smoke'] and read(path)['protocol_sha256']==sha(OUT/'protocol.json')
            else:pending.append((cid,pi,path))
    if not pending:
        print(f'{phase}: all batches verified; no generation',flush=True);return
    model,tokenizer=steering.load_target(str(ROOT/protocol['models']['target']['path']),'cuda:0')
    fixtures=read(OUT/'smoke_fixtures.json')['concepts']
    for cid,pi,path in pending:
        arms=arms_for(cid,pi,actual_phase)
        rng=seed(cid,'smoke' if smoke else phase,pi)
        torch.manual_seed(rng);torch.cuda.manual_seed_all(rng)
        started=time.monotonic()
        concept=pool['concepts'].get(str(cid),fixtures.get(str(cid)))
        assert concept is not None
        responses=generate_batch(model,tokenizer,prompts[pi]['text'],concept,arms)
        torch.cuda.synchronize()
        row={'concept_id':cid,'phase':'smoke' if smoke else phase,'prompt_index':pi,'prompt':prompts[pi]['text'],
             'generation_arms':responses,'arm_metadata':{a:{'alpha':alpha,'direction_sha256':None if d is None else signature(d.tolist())} for a,(d,alpha) in arms.items()},
             'seed':rng,'protocol_sha256':sha(OUT/'protocol.json'),'target_revision':protocol['models']['target']['revision'],
             'elapsed_seconds':time.monotonic()-started,'smoke':smoke,'peak_allocated_bytes':torch.cuda.max_memory_allocated()}
        if not smoke and phase in ['screen','evaluation']:row['selected_strengths_sha256']=sha(OUT/'calibration'/f'c{cid}.json')
        if not smoke and phase=='evaluation':row['panels_sha256']=sha(OUT/'panels_frozen.json')
        row['batch_integrity_sha256']=signature(row)
        save(path,row)
        if not smoke:validate_batch(path,cid,phase,pi,prompts[pi]['text'],sha(OUT/'protocol.json'))
        print(f'{phase} c{cid} p{pi} saved {sum(map(len,responses.values()))} responses, {row["elapsed_seconds"]:.2f}s',flush=True)
    del model,tokenizer;unload()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=['shared','construct','nla','generate','validate','smoke'],required=True)
    parser.add_argument('--phase',choices=['tuning','screen','evaluation'],default='tuning')
    parser.add_argument('--shard',type=int,choices=[0,1],default=0)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--fixtures',action='store_true')
    args=parser.parse_args()
    protocol,pool,prompts,terms=load_inputs()
    CACHE.mkdir(parents=True,exist_ok=True)
    # Locks are outside committed artifacts and shared by every stage on a physical GPU.
    lock=(CACHE/f'gpu{os.environ.get("CUDA_VISIBLE_DEVICES","0")}.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if args.fixtures:
        ids=read(OUT/'smoke_fixtures.json')['concept_ids'][args.shard::2]
        assert ids and all(cid in read(OUT/'exclusions.json')['excluded_ids'] for cid in ids)
    else:
        ids=(read(OUT/'panels_frozen.json')['union_ids'] if args.phase=='evaluation' else pool['concept_ids'])[args.shard::2]
    if args.limit:ids=ids[:args.limit]
    started=time.time()
    receipt={'stage':args.stage,'phase':args.phase,'shard':args.shard,'concept_ids':ids,'started_unix':started,'gpu':torch.cuda.get_device_name(0),'status':'running'}
    path=OUT/'runtime'/f'{args.stage}.{args.phase}.shard{args.shard}.{time.time_ns()}.json'
    try:
        if args.stage=='shared':shared(args.phase)
        elif args.stage=='construct':construct(ids)
        elif args.stage=='nla':prepare_directions(ids,args.phase)
        elif args.stage=='generate':generate(ids,args.phase)
        elif args.stage=='validate':validate_shard(args.phase,args.shard,ids)
        elif args.stage=='smoke':generate(ids,'tuning',smoke=True)
        receipt['status']='complete'
    except BaseException:
        receipt.update(status='failed',error=traceback.format_exc());raise
    finally:
        receipt.update(finished_unix=time.time(),elapsed_seconds=time.time()-started,peak_allocated_bytes=torch.cuda.max_memory_allocated(),peak_reserved_bytes=torch.cuda.max_memory_reserved())
        save(path,receipt)

if __name__=='__main__':main()
