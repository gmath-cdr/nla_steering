"""Analyze A_fresh/B/C; keep historical A comparisons explicitly separate."""
import itertools
import json
import time
import numpy as np
from analyze_explanation_source_ablation import ci
from audit_explanation_source_ablation import ROOT, digest, object_hash, save
from experiments.confirmation_metrics import evaluate_prompt
from prepare_seed_matched_ablation import BASE, OUT

METRICS = ['concept_hit', 'coverage', 'quality_pass']
ARMS = ['A_fresh', 'B', 'C', 'A_archived']


def main():
    started = time.monotonic()
    manifest = json.loads((OUT/'manifest.json').read_text())
    assert digest(BASE/'manifest.json') == manifest['historical_manifest_sha256']
    for name, sha in manifest['historical_sha256'].items():
        assert digest(BASE/name) == sha, name
    assert digest(OUT/'source_target_mapping.json') == manifest['mapping_sha256']
    fresh = json.loads((OUT/'source_target_mapping.json').read_text())
    historical = json.loads((BASE/'source_target_mapping.json').read_text())
    mapping = {(m['condition'],m['concept_id'],m['target_prompt_index']):m for m in fresh+historical}
    held=ROOT/'artifacts/paper_runs/data/baseline_panel14/qwen_nla'
    archive={(r['concept_id'],r['prompt_index']):r for r in json.loads((held/'transcripts.json').read_text())}
    lexicons=json.loads((held/'lexicons.json').read_text())
    ids=manifest['archived_protocol']['concept_ids']
    arrays={a:{m:[] for m in METRICS} for a in ARMS}
    scored=[]; raw=[]; keys=set()
    for cid in ids:
        values={a:{m:[] for m in METRICS} for a in ARMS}
        for i in range(8):
            ref=archive[cid,i]
            arms={'A_archived':ref['generation_arms']['av_edit'], 'unsteered':ref['generation_arms']['unsteered']}
            for arm in ARMS[:3]:
                directory=OUT if arm=='A_fresh' else BASE
                batch=json.loads((directory/'batches'/f'{arm}.c{cid}.p{i}.json').read_text())
                source=mapping[arm,cid,i]
                assert batch['mapping_sha256']==object_hash(source)
                assert digest(ROOT/source['archived_three_rollout_vector'])==batch['source_vector_sha256']==source['archived_three_rollout_vector_sha256']
                assert batch['generation_settings_sha256']==object_hash(manifest['generation_settings'])
                assert batch['target_revision']==manifest['models']['target'][0]['revision']
                assert batch['seed']==42+cid*10000+(14+i)*10
                assert batch['alpha']==ref['alpha']==manifest['strengths'][str(cid)]
                assert batch['prompt']==ref['prompt']
                assert (batch['condition'],batch['concept_id'],batch['prompt_index'],batch['global_prompt_index'])==(arm,cid,i,14+i)
                arms[arm]=batch['responses']
                for j,response in enumerate(batch['responses']):
                    raw.append(dict(condition=arm,concept_id=cid,global_prompt_index=14+i,prompt_index=i,sample_index=j,response=response,seed=batch['seed'],prompt=ref['prompt']))
            evaluated=evaluate_prompt({'prompt_index':i,'generation_arms':arms},lexicons[str(cid)])
            for arm in ARMS:
                scores=evaluated['arms'][arm]
                assert len(scores)==len(arms[arm])==2
                values[arm]['coverage'].append([int(any(s['concept_hit'] for s in scores))])
                for metric in ['concept_hit','quality_pass']:
                    values[arm][metric].append([int(s[metric]) for s in scores])
                for j,score in enumerate(scores):
                    key=(arm,cid,i,j)
                    assert key not in keys
                    keys.add(key)
                    scored.append(dict(condition=arm,concept_id=cid,prompt_index=i,sample_index=j,**score))
        for arm in ARMS:
            for metric in METRICS:
                arrays[arm][metric].append(values[arm][metric])
    assert len(keys)==896 and len(raw)==672
    assert len(list((OUT/'batches').glob('*.json')))==112
    arrays={a:{m:np.asarray(v,float) for m,v in d.items()} for a,d in arrays.items()}
    def summary(arms):
        return {a:{m:{'percent':float(arrays[a][m].mean()*100),'ci95_percent':ci(arrays[a][m],'concept_cluster')['ci95_percentage_points']} for m in METRICS} for a in arms}
    def differences(pairs,keep=None):
        return {f'{a}_minus_{b}':{m:ci((arrays[a][m]-arrays[b][m]) if keep is None else (arrays[a][m]-arrays[b][m])[keep],'concept_cluster') for m in METRICS} for a,b in pairs}
    pairs=list(itertools.combinations(ARMS[:3],2))
    archive_pairs=[('A_archived',a) for a in ARMS[:3]]
    metrics={'aggregate':summary(ARMS[:3]),'paired_differences':differences(pairs),
             'bootstrap':'14 paired concept clusters, percentile 95% CI, 10000 resamples, seed 0; unadjusted exploratory intervals',
             'quality_reference':'closest archived prompt-matched unsteered response; frozen 8–350 words, repetition <=0.10, content Jaccard >=0.12',
             'limitation':manifest['limitation']}
    save(OUT/'metrics.json',metrics)
    save(OUT/'archived_A_comparison.json',{'limitation':'Historical A target revision and sampling RNG unknown; not seed matched.', 'aggregate':summary(['A_archived']),'differences':differences(archive_pairs)})
    loco=[{'excluded_concept_id':cid,'differences':differences(pairs,[j for j in range(14) if j!=index])} for index,cid in enumerate(ids)]
    save(OUT/'leave_one_concept_out.json',loco)
    save(OUT/'per_concept_metrics.json',{str(cid):{a:{m:float(arrays[a][m][j].mean()*100) for m in METRICS} for a in ARMS} for j,cid in enumerate(ids)})
    save(OUT/'scored_responses.json',scored)
    with (OUT/'raw_A_fresh.jsonl').open('w') as f:
        for row in raw:
            if row['condition']=='A_fresh': f.write(json.dumps(row)+'\n')
    save(OUT/'counts.json',{'responses_per_primary_condition':224,'prompts_per_condition':112,'primary_unique_response_keys':len({(r['condition'],r['concept_id'],r['prompt_index'],r['sample_index']) for r in raw}), 'total_primary_responses':672,'A_fresh_batches':112,'all_scored_unique_keys_including_archived_A':len(keys),'complete':True})
    workers=[json.loads((OUT/f'worker{i}_runtime.json').read_text()) for i in range(2)]
    assert all(w['status']=='complete' and w['responses']==112 and len(w['concept_ids'])==7 for w in workers)
    assert set(workers[0]['concept_ids']).isdisjoint(workers[1]['concept_ids'])
    wall=max(w['finished_unix'] for w in workers)-min(w['started_unix'] for w in workers)
    save(OUT/'runtime.json',{'workers':workers,'generation_wall_seconds':wall,'analysis_seconds':time.monotonic()-started})
    lines=['# Question 3: seed-matched explanation-source ablation','', '| Condition | Concept-word incidence % (95% CI) | Prompt coverage % (95% CI) | Quality pass % (95% CI) |','|---|---:|---:|---:|']
    for a,d in metrics['aggregate'].items():
        cells=[f"{d[m]['percent']:.2f} [{d[m]['ci95_percent'][0]:.2f}, {d[m]['ci95_percent'][1]:.2f}]" for m in METRICS]
        lines.append('| '+a+' | '+' | '.join(cells)+' |')
    lines+=['','| Difference | Metric | Change pp | 95% concept-cluster CI | LOCO point range pp |','|---|---|---:|---|---|']
    for pair,d in metrics['paired_differences'].items():
        for m,v in d.items():
            lo,hi=v['ci95_percentage_points']; vals=[r['differences'][pair][m]['difference_percentage_points'] for r in loco]
            lines.append(f"| {pair} | {m} | {v['difference_percentage_points']:+.2f} | [{lo:+.2f}, {hi:+.2f}] | [{min(vals):+.2f}, {max(vals):+.2f}] |")
    lines+=['','All primary arms: 14 concepts × held-out global prompts 14–21 × two responses = 224 responses each, 672 unique primary keys. A_fresh uses each current target prompt’s archived explanation bundle and reconstructed three-rollout direction. B uses the cyclic next held-out prompt; C uses calibration global prompt 10. Strengths and generation settings are unchanged. No new AV/AR inference.', '', 'Environment: A_fresh uses PyTorch 2.9.1+cu128 and Transformers 4.57.1; historical B/C recorded PyTorch 2.13.0 and Transformers 4.57.6. Both use accelerate 1.14.0. See manifest.json for full provenance.', '', 'Common seed per two-response batch: `42 + concept_id * 10000 + target_global_prompt_index * 10`. Target revision: `a09a35458c702b33eeacc393d103063234e8bc28`. '+manifest['limitation'], '', metrics['bootstrap']+'. Coverage means at least one concept-word hit among the two responses. Quality is an automated proxy against archived unsteered references, not a new independent quality judgment. Full paired and LOCO CIs are in the JSON results.', '', '**Archived A is separate:** `archived_A_comparison.json` contains its rates and differences; its original revision and generation seeds are unknown. The original experiment directory’s files were hash-verified unchanged.', '', f'Generation wall time: {wall:.2f} seconds ({wall/60:.2f} minutes), seven concepts on each of two A100 GPUs. Completed checkpoints are verified and skipped on resume.', '', 'Commands (from repository root):', '```bash','PYTHONPATH=src/nla_inference .venv/bin/python scripts/prepare_seed_matched_ablation.py','CUDA_VISIBLE_DEVICES=0 PYTHONPATH=src/nla_inference HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/run_explanation_source_ablation.py --fresh --shard 0','CUDA_VISIBLE_DEVICES=1 PYTHONPATH=src/nla_inference HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/run_explanation_source_ablation.py --fresh --shard 1','PYTHONPATH=src/nla_inference .venv/bin/python scripts/analyze_seed_matched_ablation.py','```','','Outputs: `artifacts/experiments/nla_explanation_source_panel14/seed_matched/`. Both workers ran concurrently. No model weights or caches are included.']
    intervals=[metrics['paired_differences'][n]['concept_hit']['ci95_percentage_points'] for n in ['A_fresh_minus_B','A_fresh_minus_C']]
    finding='Current-prompt incidence exceeds both alternatives with both intervals above zero.' if all(v[0]>0 for v in intervals) else 'The incidence intervals do not establish a current-prompt advantage over both alternatives; this does not establish equivalence.'
    comparisons = [metrics['paired_differences'][n] for n in ['A_fresh_minus_B','A_fresh_minus_C']]
    if all(d['coverage']['ci95_percentage_points'][0] > 0 and d['quality_pass']['ci95_percentage_points'][1] < 0 for d in comparisons):
        finding += ' A_fresh has higher prompt coverage and lower automated quality-pass rates than both alternatives, with the corresponding paired intervals excluding zero.'
    finding += ' These are lexical-steering results; the quality tradeoff must be considered separately.'
    lines[2:2]=[finding,'']
    (OUT/'RESULT_SEED_MATCHED.md').write_text('\n'.join(lines)+'\n')
    manifest.update(status='complete; counts, uniqueness, historical integrity and seed receipts verified',completed_unix=time.time(),runtime=json.loads((OUT/'runtime.json').read_text()))
    manifest['implementation_hashes']['scripts/analyze_seed_matched_ablation.py']=digest(ROOT/'scripts/analyze_seed_matched_ablation.py')
    save(OUT/'manifest.json',manifest)
    with (OUT/'SHA256SUMS').open('w') as f:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and p.name!='SHA256SUMS' and p.suffix not in ['.log','.tmp','.lock']:
                f.write(f'{digest(p)}  {p.relative_to(OUT)}\n')
    print(json.dumps(metrics,indent=2))

if __name__=='__main__': main()
