"""Frozen selection boundary and overlap-aware concept-cluster interaction analysis."""
import argparse
import csv
import time
import numpy as np
from symmetric_common import *


def merge_validation(phase,ids):
    receipts=[read(OUT/'validation'/f'{phase}.shard{s}.json') for s in [0,1]]
    combined=[]
    for r in receipts:
        assert r['validated'] and r['response_count']==r['unique_response_keys']
        for name,digest in r['sha256'].items():assert sha(ROOT/name)==digest,name
        combined+=r['concept_ids']
    assert len(combined)==len(set(combined))==len(ids) and set(combined)==set(ids)
    return receipts


def freeze_panels():
    protocol,pool,prompts,terms=load_inputs()
    ids=pool['concept_ids']
    merge_validation('tuning',ids);merge_validation('screen',ids)
    rankings={}
    for method in ['nla','diffmean']:
        rows=[{'concept_id':cid,**read(OUT/'screen_metrics'/f'c{cid}.json')['metrics'][method]} for cid in ids]
        rankings[method]=sorted(rows,key=lambda r:(-r['quality_eligible'],-r['selection_score'],-r['hit_rate'],r['concept_id']))
    panels={method:[r['concept_id'] for r in ranks[:20]] for method,ranks in rankings.items()}
    union=[cid for cid in ids if any(cid in panel for panel in panels.values())]
    value={'protocol_sha256':sha(OUT/'protocol.json'),'panels':panels,'union_ids':union,
           'overlap_ids':sorted(set(panels['nla'])&set(panels['diffmean'])),
           'screen_hashes':{str(cid):sha(OUT/'screen_metrics'/f'c{cid}.json') for cid in ids},
           'calibration_hashes':{str(cid):sha(OUT/'calibration'/f'c{cid}.json') for cid in ids},
           'rankings':rankings,'quality_eligible_panel_counts':{m:sum(r['quality_eligible'] for r in ranks[:20]) for m,ranks in rankings.items()}}
    assert all(len(set(panel))==20 for panel in panels.values())
    if (OUT/'panels_frozen.json').exists():
        original=read(OUT/'panels_frozen.json');assert {k:v for k,v in original.items() if k!='frozen_unix'}==value
    else:
        assert not list((OUT/'generations'/'evaluation').glob('*.json')), 'Held-out results exist before panel freeze'
        assert not (OUT/'shared'/'evaluation.complete.json').exists()
        save(OUT/'panels_frozen.json',value|{'frozen_unix':time.time()})
    freeze(OUT/'strengths_frozen.json',{'protocol_sha256':sha(OUT/'protocol.json'),
           'selected':{str(cid):read(OUT/'calibration'/f'c{cid}.json')['selected'] for cid in ids},
           'calibration_hashes':value['calibration_hashes']})
    print(f'Panels frozen: 20 each, {len(union)} unique, {len(value["overlap_ids"])} shared',flush=True)


def bootstrap_indices(ids,panels,resamples=10000,seed_value=0):
    """Shared draws within membership strata preserve paired/overlap covariance."""
    rng=np.random.default_rng(seed_value)
    index={cid:i for i,cid in enumerate(ids)}
    draws={m:[] for m in ['nla','diffmean']}
    groups={}
    for cid in ids:
        membership=tuple(m for m in ['nla','diffmean'] if cid in panels[m])
        if membership:groups.setdefault(membership,[]).append(index[cid])
    for membership,indices in groups.items():
        sample=rng.choice(indices,size=(resamples,len(indices)),replace=True)
        for method in membership:draws[method].append(sample)
    return {m:np.concatenate(v,axis=1) for m,v in draws.items()}


def interval(point,draws):
    return {'value_percentage_points':float(point*100),'ci95_percentage_points':[float(x*100) for x in np.quantile(draws,[.025,.975])]}


def interaction_results(ids,panels,values):
    index={cid:i for i,cid in enumerate(ids)}
    draws=bootstrap_indices(ids,panels)
    result={}
    for metric in METRICS:
        delta=np.array([values[cid]['diffmean'][metric]-values[cid]['nla'][metric] for cid in ids])
        means={m:float(delta[[index[c] for c in panels[m]]].mean()) for m in panels}
        boots={m:delta[draws[m]].mean(axis=1) for m in panels}
        result[metric]=interval(means['diffmean']-means['nla'],boots['diffmean']-boots['nla'])
    return result,draws


def analyze():
    protocol,pool,prompts,terms=load_inputs()
    frozen=read(OUT/'panels_frozen.json');ids=frozen['union_ids'];panels=frozen['panels']
    for cid in pool['concept_ids']:
        assert sha(OUT/'screen_metrics'/f'c{cid}.json')==frozen['screen_hashes'][str(cid)]
        assert sha(OUT/'calibration'/f'c{cid}.json')==frozen['calibration_hashes'][str(cid)]
    merge_validation('evaluation',ids)
    values={cid:read(OUT/'evaluation_metrics'/f'c{cid}.json')['metrics'] for cid in ids}
    interaction,draws=interaction_results(ids,panels,values)
    index={cid:i for i,cid in enumerate(ids)}
    arms=expected_arms('evaluation')
    per_panel={};table=[]
    for selector,panel in panels.items():
        metrics={}
        for arm in arms:
            metrics[arm]={}
            for metric in METRICS:
                data=np.array([values[cid][arm][metric] for cid in ids])
                point=data[[index[c] for c in panel]].mean()
                estimate=interval(point,data[draws[selector]].mean(axis=1));metrics[arm][metric]=estimate
                table.append({'panel':selector,'contrast':arm,'metric':metric,**estimate})
        delta={}
        for metric in METRICS:
            data=np.array([values[cid]['diffmean'][metric]-values[cid]['nla'][metric] for cid in ids])
            delta[metric]=interval(data[[index[c] for c in panel]].mean(),data[draws[selector]].mean(axis=1))
            table.append({'panel':selector,'contrast':'diffmean_minus_nla','metric':metric,**delta[metric]})
        per_panel[selector]={'concept_count':20,'responses_per_method':320,'metrics':metrics,'paired_difference':delta}
    per_concept=[]
    for cid in ids:
        per_concept.append({'concept_id':cid,'concept':pool['concepts'][str(cid)],'nla_selected':cid in panels['nla'],'diffmean_selected':cid in panels['diffmean'],
                            'metrics':values[cid],'paired_difference':{m:values[cid]['diffmean'][m]-values[cid]['nla'][m] for m in METRICS}})
    loco=[]
    for cid in ids:
        keep=[c for c in ids if c!=cid]
        reduced={m:[c for c in panel if c!=cid] for m,panel in panels.items()}
        result,_=interaction_results(keep,reduced,values)
        loco.append({'excluded_concept_id':cid,'panel_sizes':{m:len(p) for m,p in reduced.items()},'interaction':result})
    counts={phase:sum(read(OUT/'validation'/f'{phase}.shard{s}.json')['response_count'] for s in [0,1]) for phase in ['tuning','screen','evaluation']}
    assert counts=={'tuning':14400,'screen':9600,'evaluation':len(ids)*112}
    all_keys=set()
    with (OUT/'held_out_generations.jsonl').open('w') as stream:
        for cid in ids:
            for pi in phase_indices('evaluation'):
                row=read(task_path('evaluation',cid,pi))
                for arm,responses in row['generation_arms'].items():
                    for sample,response in enumerate(responses):
                        key=(cid,pi,arm,sample);assert key not in all_keys;all_keys.add(key)
                        stream.write(json.dumps({'concept_id':cid,'prompt_index':pi,'arm':arm,'sample_index':sample,'prompt':row['prompt'],
                             'rendered_user_prompt':row['prompt']+ ('\n\nPlease make your answer specifically concern: '+pool['concepts'][str(cid)]+'.' if arm=='prompting' else ''),
                             'response':response,'seed':row['seed'],'alpha':row['arm_metadata'][arm]['alpha']})+'\n')
    assert len(all_keys)==counts['evaluation']
    result={'study':'prospective symmetric self-selection','primary_metric':'hit_rate','primary_interaction':interaction['hit_rate'],
            'interaction_definition':protocol['primary'],'secondary_interactions':{m:v for m,v in interaction.items() if m!='hit_rate'},
            'per_panel':per_panel,'overlap_count':len(frozen['overlap_ids']),'counts':counts,
            'unique_held_out_keys':len(all_keys),'bootstrap':protocol['bootstrap'],'limitations':protocol['limitations'],
            'primary_excludes_contextual_controls':True,'protocol_sha256':sha(OUT/'protocol.json'),'panels_sha256':sha(OUT/'panels_frozen.json')}
    save(OUT/'result.json',result);save(OUT/'per_concept_results.json',per_concept);save(OUT/'leave_one_concept_out.json',loco)
    for metric,estimate in interaction.items():table.append({'panel':'interaction','contrast':'difference_of_paired_differences','metric':metric,**estimate})
    with (OUT/'result.csv').open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=['panel','contrast','metric','value_percentage_points','ci95_lower','ci95_upper'],lineterminator='\n');writer.writeheader()
        for row in table:
            lower,upper=row.pop('ci95_percentage_points');writer.writerow({**row,'ci95_lower':lower,'ci95_upper':upper})
    with (OUT/'paired_concept_differences.csv').open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=['concept_id','nla_selected','diffmean_selected',*METRICS],lineterminator='\n');writer.writeheader()
        for r in per_concept:writer.writerow({k:r[k] for k in ['concept_id','nla_selected','diffmean_selected']}|{m:r['paired_difference'][m]*100 for m in METRICS})
    runtimes=[read(p) for p in (OUT/'runtime').glob('*.json')]
    failed=[r for r in runtimes if r['status']!='complete']
    elapsed=max(r['finished_unix'] for r in runtimes)-min(r['started_unix'] for r in runtimes)
    runtime={'wall_seconds':elapsed,'active_worker_seconds':sum(r['elapsed_seconds'] for r in runtimes),
             'peak_allocated_bytes':max(r['peak_allocated_bytes'] for r in runtimes),'failed_attempts':failed}
    save(OUT/'runtime_summary.json',runtime)
    primary=interaction['hit_rate'];lower,upper=primary['ci95_percentage_points']
    interpretation=('The positive interaction supports selection-method dependence of lexical steering on these frozen panels.' if lower>0 else
                    'The negative interaction runs against the predicted selection-method dependence on these frozen panels.' if upper<0 else
                    'The interaction interval includes zero; this run does not establish selection-method dependence or equivalence.')
    lines=['# Prospective symmetric self-selection (reviewer Question 2)','',interpretation,'',
           f'Primary interaction: **{primary["value_percentage_points"]:+.2f} percentage points**, concept-cluster 95% CI **[{lower:+.2f}, {upper:+.2f}]**.',
           '', '| Selector panel | NLA incidence % | DiffMean incidence % | DiffMean − NLA pp (95% CI) | NLA quality % | DiffMean quality % | NLA coverage % | DiffMean coverage % |',
           '|---|---:|---:|---|---:|---:|---:|---:|']
    for selector,panel in per_panel.items():
        def val(arm,metric):return panel['metrics'][arm][metric]['value_percentage_points']
        d=panel['paired_difference']['hit_rate'];lo,hi=d['ci95_percentage_points']
        lines.append(f'| {selector} | {val("nla","hit_rate"):.2f} | {val("diffmean","hit_rate"):.2f} | {d["value_percentage_points"]:+.2f} [{lo:+.2f}, {hi:+.2f}] | {val("nla","quality_pass_rate"):.2f} | {val("diffmean","quality_pass_rate"):.2f} | {val("nla","prompt_coverage"):.2f} | {val("diffmean","prompt_coverage"):.2f} |')
    lo_values=[r['interaction']['hit_rate']['value_percentage_points'] for r in loco]
    lines+=['',f'The panels each contain 20 concepts, with {len(frozen["overlap_ids"])} shared concepts. Leave-one-concept-out interaction estimates range from {min(lo_values):+.2f} to {max(lo_values):+.2f} pp. Full sensitivity intervals and paired concept differences are saved in JSON/CSV.',
            '',f'Pool: fixed seed 20261018, 100 of {pool["eligible_count"]} eligible concepts; prior numerical scores were not used. Exclusion scope and membership provenance are in exclusions.json. Twenty newly authored prompts were checked against prior tracked artifacts: 4 tuning, 8 screening, 8 held out. No held-out target generation or NLA explanation preparation occurred before the two panels froze.',
            '', 'Both methods use the same model revision, lexical lists, quality checks, strengths, tuning score, selection rank, random controls, sampling settings, and last-token injection window. The direction constructions differ: NLA uses three prompt-specific edited explanation deltas; DiffMean uses 72 construction examples. Quality-ineligible strength fallbacks and panel members are retained and flagged, preserving exactly 20 selections per method.',
            '', 'The primary endpoint is lexical concept-word incidence. Quality-adjusted incidence, selection scores, quality-pass rates, coverage, all per-panel intervals, and contextual prompting/unsteered/random scores are in result.json/result.csv. Prompting and unsteered outputs do not enter the primary interaction. Random controls enter the frozen tuning/ranking score, not the held-out primary contrast.',
            '', 'Bootstrap: 10,000 paired concept clusters, seed 0, percentile 95% intervals, stratified by panel membership. Shared concepts are resampled together across methods and panels; the panel overlap is not treated as independent observations. Intervals condition on this pool, tuning, and the selected panels; they do not quantify uncertainty from repeated selection campaigns. A positive interaction need not imply that each method wins its own panel.',
            '', 'This is one prospective, symmetrically selected lexical-steering study on Qwen2.5-7B. It does not establish general superiority, semantic specificity, human-rated quality, or a causal effect of panel selection across all models. Previously screened concepts are allowed; earlier scores and outputs are not reused as new outcomes. The six-concept mirror remains historical context only.',
            '',f'Responses: tuning {counts["tuning"]:,}; screening {counts["screen"]:,}; held out {counts["evaluation"]:,} unique keys. Both shards were validated before merge. Wall time {elapsed/3600:.2f} hours on two A100s; peak allocated {runtime["peak_allocated_bytes"]/2**30:.2f} GiB. Resumable atomic batches, raw generations, calibration results, rankings, models, environment, seeds and hashes are preserved.',
            '', 'Reproduce: `.venv/bin/python scripts/coordinate_symmetric_selection.py`; analyze: `.venv/bin/python scripts/analyze_symmetric_selection.py --action analyze`. Model weights and temporary tensor caches stay outside Git.']
    (OUT/'RESULT.md').write_text('\n'.join(lines)+'\n')
    manifest=read(OUT/'manifest.json');manifest.update(status='complete',completed_unix=time.time(),result_sha256=sha(OUT/'result.json'),counts=counts,runtime=runtime)
    save(OUT/'manifest.json',manifest)
    with (OUT/'SHA256SUMS').open('w') as stream:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and p.name!='SHA256SUMS' and p.suffix not in ['.log','.tmp','.lock']:
                stream.write(f'{sha(p)}  {p.relative_to(OUT)}\n')
    print(json.dumps({'primary_interaction':primary,'counts':counts,'wall_hours':elapsed/3600}),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--action',choices=['freeze_panels','analyze'],default='analyze')
    args=parser.parse_args();{'freeze_panels':freeze_panels,'analyze':analyze}[args.action]()

if __name__=='__main__':main()
