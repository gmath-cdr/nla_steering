"""Freeze the prospective symmetric experiment before generating any outcomes."""
import argparse
import importlib.metadata
import json
import random
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from symmetric_common import ROOT, OUT, GRID, RANDOM, sha, save, freeze, read

PROMPTS = [
    'For a beginner with one free afternoon, explain how to turn a vague personal goal into three small actions and one practical check of progress.',
    'Describe a sensible way for two neighbors to resolve a minor disagreement about a shared space, including what each should do before meeting.',
    'Explain how to compare two everyday options when neither is clearly better. Give a short example and state what information could change the choice.',
    'Write a practical guide for someone who wants to learn an unfamiliar skill in short weekly sessions, including a way to notice recurring mistakes.',
    'A small group keeps starting useful projects but rarely finishes them. Suggest a realistic process for choosing one project and carrying it through.',
    'Explain why a familiar routine can stop working after circumstances change, and describe how someone could revise it without changing everything at once.',
    'Give a balanced explanation of how to decide whether a surprising claim deserves further attention, using a simple invented example.',
    'Describe how to organize a shared task when people have different levels of experience and only a limited amount of time together.',
    'Someone receives conflicting advice from two trusted friends. Explain a careful way to understand the disagreement and decide what to do next.',
    'Write a brief guide to making a useful checklist for an ordinary recurring task, including how to keep the checklist from becoming cumbersome.',
    'Explain how to recover constructively after a modest plan fails, distinguishing what can be learned from what remains uncertain.',
    'Describe how a person could prepare for a conversation in which they need to explain a difficult choice clearly and answer reasonable questions.',
    'A volunteer group wants to improve one aspect of its weekly activity. Describe how it could try a small change and decide whether to keep it.',
    'Explain how to break an unfamiliar problem into smaller parts while preserving a clear view of the overall purpose, with an everyday example.',
    'Write a thoughtful answer about how to balance immediate convenience against a possible longer term benefit when the future is uncertain.',
    'Describe a method for revising a rough first draft of a practical explanation so that a reader can understand and use it more easily.',
    'Someone wants to ask for help but does not know what details to provide. Explain how they could describe the situation and make a clear request.',
    'Explain how to evaluate whether a new habit is useful after trying it for several weeks, including how to account for unusually good or bad days.',
    'Describe how a team could keep a simple record of decisions so that a person joining later can understand the reasons and remaining questions.',
    'Give a practical explanation of how to choose what to do first when several small responsibilities compete for attention on a busy day.',
]


def exclusion_catalog():
    audit=read(ROOT/'artifacts/experiments/prospective_symmetric_self_selection/eligibility_audit.json')
    ids=set();sources=[]
    for study in audit['prior_selection_studies']:
        # Only pre-recorded membership flags/IDs are consumed, never numerical scores.
        ids.update(study['shortlisted_candidate_ids'])
        sources.append({'path':study['path'],'sha256':study['sha256'],'ids':study['shortlisted_candidate_ids'],'type':'Qwen shortlist'})
    assert len(ids)==235
    extra=set()
    for path in sorted((ROOT/'artifacts/paper_runs/data').rglob('protocol.json')):
        data=read(path)
        current=data.get('concept_ids',[])
        if isinstance(current,list) and 0<len(current)<500 and all(isinstance(c,int) and 0<=c<500 for c in current):
            extra.update(current)
            sources.append({'path':str(path.relative_to(ROOT)),'sha256':sha(path),'ids':current,'type':'prior finite panel or confirmation'})
    paths=[ROOT/'artifacts/paper_runs/data/gemma3-12b/discovery500_full_2026_merged/confirmation_candidates.json',
           ROOT/'artifacts/paper_runs/data/outcome_blind_axb44/strict_lexical/blind10_lexicons_v2.json',
           ROOT/'artifacts/paper_runs/data/cross_model_extended_2026-08-08/protocol/concept7_terms.json']
    for path in paths:
        data=read(path)
        current=data.get('selected_concept_ids') if 'selected_concept_ids' in data else list(map(int,data.get('lexicons',data)))
        extra.update(current)
        sources.append({'path':str(path.relative_to(ROOT)),'sha256':sha(path),'ids':current,'type':'reported panel or shortlist'})
    for path in sorted((ROOT/'artifacts/experiments').glob('diffmean*/panel_frozen.json')):
        current=read(path)['concept_ids'];extra.update(current)
        sources.append({'path':str(path.relative_to(ROOT)),'sha256':sha(path),'ids':current,'type':'prior mirror selected panel'})
    return ids,extra,sources


def freshness_audit():
    canonical=lambda s:' '.join(s.lower().split())
    targets=[canonical(p) for p in PROMPTS]
    names=subprocess.check_output(['git','ls-files','artifacts'],cwd=ROOT,text=True).splitlines()
    checked={};matches=[]
    def check(data,name):
        text=canonical(data.decode('utf-8',errors='replace'))
        for index,prompt in enumerate(targets):
            if prompt in text: matches.append({'prompt_index':index,'path':name})
    for name in names:
        path=ROOT/name
        if name.startswith(str(OUT.relative_to(ROOT))+'/'):continue
        if path.suffix in ['.json','.jsonl','.csv','.md','.txt','.py']:
            check(path.read_bytes(),name);checked[name]=sha(path)
        elif path.name.endswith(('.tar.gz','.tgz','.tar')):
            with tarfile.open(path,'r:*') as archive:
                for member in archive:
                    if member.isfile() and Path(member.name).suffix in ['.json','.jsonl','.csv','.txt','.md']:
                        check(archive.extractfile(member).read(),name+'::'+member.name)
            checked[name]=sha(path)
    assert not matches, matches
    return {'rule':'Exact case/whitespace-normalized authored prompt text absent from all tracked prior text artifacts and text members of tar archives',
            'matches':matches,'checked_files_sha256':checked,'prompt_count':len(PROMPTS),'fresh':True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scope',choices=['qwen235','all_reported'],required=True)
    args=parser.parse_args()
    if (OUT/'freeze.json').exists():
        from symmetric_common import verify_freeze
        verify_freeze();print('Existing frozen protocol verified; no changes.');return
    qwen,extra,sources=exclusion_catalog()
    excluded=qwen if args.scope=='qwen235' else qwen|extra
    eligible=sorted(set(range(500))-excluded)
    rng=random.Random(20261018)
    pool=rng.sample(eligible,100)
    metadata=ROOT/'src/axbench/axbench/concept500/prod_9b_l20_v1/generate/metadata.jsonl'
    labels={int(r['concept_id']):r['concept'] for r in map(json.loads,metadata.read_text().splitlines())}
    from experiments.discovery500 import lexical_terms
    OUT.mkdir(parents=True,exist_ok=True)
    save(OUT/'exclusions.json',{'scope':args.scope,'excluded_ids':sorted(excluded),'eligible_ids':eligible,
         'qwen_shortlist_union_count':235,'additional_reported_ids_outside_qwen_union':sorted(extra-qwen),'sources':sources,
         'selection_uses_prior_scores':False})
    smoke_ids = sorted(excluded)[:2]
    assert len(smoke_ids) == 2 and not (set(smoke_ids) & set(pool))
    save(OUT/'smoke_fixtures.json',{
        'concept_ids': smoke_ids,
        'concepts': {str(cid): labels[cid] for cid in smoke_ids},
        'excluded_from_pool': True,
        'selection_and_evaluation_use': False,
        'purpose': 'Disposable one-concept-per-GPU fixtures for pipeline and resume checks.',
    })
    save(OUT/'pool.json',{'seed':20261018,'sampler':'Python random.Random(seed).sample(sorted(eligible_ids), 100)',
         'concept_ids':pool,'eligible_count':len(eligible),'concepts':{str(cid):labels[cid] for cid in pool}})
    save(OUT/'prompts.json',[{'index':i,'split':'tuning' if i<4 else 'screen' if i<12 else 'evaluation','text':p} for i,p in enumerate(PROMPTS)])
    save(OUT/'lexicons.json',{str(cid):lexical_terms(labels[cid]) for cid in pool})
    save(OUT/'prompt_freshness_audit.json',freshness_audit())
    previous=read(ROOT/'artifacts/experiments/nla_explanation_source_panel14/manifest.json')
    models={role:{'path':rows[0]['local_path'],'revision':rows[0]['revision']} for role,rows in previous['models'].items()}
    settings={'max_new_tokens':96,'temperature':.7,'top_p':.8,'top_k':20,'repetition_penalty':1.05,'do_sample':True,'samples_per_arm':2,'dtype':'bfloat16'}
    protocol={'version':'prospective-symmetric-selection-v1','pool_seed':20261018,'pool_size':100,'panel_size':20,
        'exclusion_scope':args.scope,'eligible_count':len(eligible),'models':models,'layer':20,
        'injection':'Identical norm-scaled direction at last prompt token and each generated token for NLA, DiffMean and random controls',
        'splits':{'tuning':[0,4],'screen':[4,12],'evaluation':[12,20]},'grid':GRID,'settings':settings,
        'random_controls':{'direction_seeds':RANDOM,'alpha':.5,'rule':'unit Gaussian, seed=vector_seed*1000000+concept_id*100+prompt_index; identical controls for both methods'},
        'target_seed_rule':'20261018 + concept_id*100000 + phase_code*1000 + prompt_index*10; phase tuning=1, screen=2, evaluation=3, smoke=4',
        'batching':'Fixed arm order and two consecutive samples per arm; common prompt-batch seed, not identical per-arm RNG draws; deterministic full-batch checkpoint on resume',
        'nla':{'rollouts':3,'temperature':1.,'max_new_tokens':200,'seed_rule':'20261018+prompt_index*10+rollout_index+1',
               'construction':'Three independent fresh prompt-only AV explanations shared across concepts; concept-scripted edit; normalize each reconstructed edited-minus-original delta then normalize their sum',
               'sharing':'Unedited activation, original explanations and original reconstructions are prompt-only and cached once, without outcome-based filtering. Held-out cache is not generated until panels freeze.'},
        'diffmean':{'construction_examples':72,'construction':'positive pooled-token mean minus shared negative pooled-token mean from train_data, AxBench trim, layer20; unit normalization'},
        'quality':{'word_count':[8,350],'repeated_trigram_max':.1,'content_word_jaccard_min':.12,'reference':'closest of two fresh prompt-matched unsteered samples',
                   'eligibility':'quality-pass >=.75, mean reference similarity >=.12, mean repetition <=.1, mean matched-term count <=4'},
        'selection_score':'min(hit_rate-unsteered_hit_rate, hit_rate-max_of_3_random_hit_rates) * prompt_coverage * quality_pass_rate; no clipping',
        'tuning':'For each method/concept independently, maximize selection_score over quality-eligible strengths; tie smaller alpha. If none eligible, maximize quality_pass_rate then selection_score then smaller alpha; retain and flag fallback.',
        'ranking':'On independent screening prompts, descending quality_eligible, selection_score, hit_rate, then ascending concept_id. Take exactly top20 for each method; retain and flag any quality-ineligible panel members.',
        'evaluation':'Evaluate union of panels once; shared concepts contribute identical results to both panel means. Direct prompting and unsteered contextual only.',
        'primary':'(DiffMean minus NLA hit_rate on DiffMean-selected panel) minus (DiffMean minus NLA hit_rate on NLA-selected panel)',
        'secondary':['quality_pass_rate','prompt_coverage','quality_adjusted_hit_rate','selection_score'],
        'bootstrap':{'resamples':10000,'seed':0,'unit':'concept','method':'Membership-stratified paired cluster bootstrap: resample concept IDs within NLA-only, DiffMean-only and shared strata, preserving panel sizes and overlap; one shared draw for both methods and both panels','ci':'percentile 95%; panels and tuning treated as fixed'},
        'loco':'Omit each union concept in turn from every panel it belongs to, renormalize means and recompute interaction; no reselection',
        'limitations':['Conditional on frozen selected panels, not uncertainty over pool sampling or reselection','Single target model/revision and a single random 100-concept pool','Lexical endpoints and automated quality proxies do not establish semantic steering or overall quality','Previously screened concepts permitted; selected/shortlisted exclusions defined in exclusions.json'],
        'smoke':'Two predeclared excluded concepts, one per GPU, on the first tuning prompt with both methods/grid/controls; smoke outcomes are excluded from tuning/screen/evaluation and cannot change the frozen pool or rules'}
    save(OUT/'protocol.json',protocol)
    training=ROOT/'src/axbench/axbench/concept500/prod_9b_l20_v1/generate/train_data.parquet'
    checkpoint_hashes={}
    for role,model in models.items():
        directory=ROOT/model['path']
        checkpoint_hashes[role]={p.name:sha(p) for p in sorted(directory.iterdir()) if p.is_file() and p.suffix in ['.json','.yaml','.safetensors','.txt','.model','.jinja']}
    save(OUT/'model_file_hashes.json',checkpoint_hashes)
    save(OUT/'environment.json',{'python':sys.version,'packages':sorted(f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions()),
          'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=index,name,uuid,memory.total,driver_version','--format=csv'],text=True),
          'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
          'cuda_visible_devices':'exactly one physical A100 per worker; concept IDs partitioned by pool order [shard::2]'})
    (OUT/'PROTOCOL.md').write_text('# Frozen prospective symmetric self-selection study\n\nAll details are fixed in protocol.json, with immutable input/code hashes in freeze.json. No target generations preceded this freeze.\n\nA seeded sample of 100 concepts uses exclusion membership only, never previous numerical scores. The same 20 newly authored neutral prompts serve every concept and both methods: four tune strengths, eight rank the two top-20 panels, eight remain held out. Prompt freshness is checked against the tracked archive, including compressed archives.\n\nThe methods differ only in direction construction: prompt-specific three-rollout NLA edits versus a fixed construction-split DiffMean vector. Layer, injection window, strengths, tuning objective, lexical terms, quality checks, random controls, sampling distribution and target revision are shared. Quality failures are retained and flagged; panels always have 20 concepts.\n\nThe primary endpoint is the difference of DiffMean-minus-NLA lexical incidence contrasts between the DiffMean-selected and NLA-selected panels. Ten thousand membership-stratified concept-cluster draws preserve overlap and pair both methods. Intervals condition on selected panels; they do not include reselection uncertainty. Direct prompting and unsteered outputs are contextual controls.\n\nNo historical outputs or scores are reused as new outcomes. Prompt-only NLA inputs can be shared because they do not depend on the edited concept. Held-out NLA preparation starts only after panel hashes freeze. Full definitions, seeds, tie breaks, fallback rules, expected counts and limitations are in protocol.json.\n')
    files=list(OUT.glob('*.json'))+[ROOT/'scripts'/n for n in ['symmetric_common.py','prepare_symmetric_selection.py','run_symmetric_selection.py','analyze_symmetric_selection.py','coordinate_symmetric_selection.py']]
    files.append(OUT/'PROTOCOL.md')
    files += [metadata,training, ROOT/'src/nla/experiments/steering.py',ROOT/'src/nla/experiments/discovery500.py',ROOT/'src/nla/experiments/confirmation_metrics.py',ROOT/'src/nla/experiments/diffmean_discovery500.py',ROOT/'src/nla/experiments/run_panel_baselines.py',ROOT/'artifacts/cache/mirror_runtime/nla_inference.py']
    files=[p for p in files if p.name not in ['freeze.json','manifest.json']]
    save(OUT/'freeze.json',{'frozen_unix':time.time(),'sha256':{str(p.relative_to(ROOT)):sha(p) for p in files}})
    save(OUT/'manifest.json',{'status':'protocol_frozen_before_outcomes','freeze_sha256':sha(OUT/'freeze.json'),'protocol_sha256':sha(OUT/'protocol.json'),'started_unix':time.time(),'held_out_started':False})
    print(f'Frozen {len(pool)} of {len(eligible)} eligible concepts -> {OUT}',flush=True)

if __name__=='__main__':main()
