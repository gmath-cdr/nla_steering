"""Shared deterministic I/O, scoring and validation for the prospective Q2 study."""
from pathlib import Path
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/experiments/prospective_symmetric_self_selection/v1'
CACHE = ROOT / 'artifacts/cache/prospective_symmetric_v1'
sys.path[:0] = [str(ROOT / 'src/nla'), str(ROOT / 'artifacts/cache/mirror_runtime'),
                str(ROOT / 'src/contrast_steering/src')]
GRID = [.1, .2, .3, .4, .5, .6, .75]
RANDOM = [17, 42, 101]
METRICS = ['hit_rate', 'prompt_coverage', 'quality_pass_rate', 'quality_adjusted_hit_rate', 'selection_score']


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def freeze(path, value):
    if path.exists():
        assert read(path) == value, f'Frozen artifact mismatch: {path}'
    else:
        save(path, value)


def verify_freeze():
    lock = read(OUT / 'freeze.json')
    for name, digest in lock['sha256'].items():
        assert sha(ROOT / name) == digest, f'Frozen input changed: {name}'
    return read(OUT / 'protocol.json')


def load_inputs():
    protocol = verify_freeze()
    return protocol, read(OUT / 'pool.json'), read(OUT / 'prompts.json'), read(OUT / 'lexicons.json')


def seed(cid, phase, pi):
    return 20261018 + cid * 100000 + {'tuning': 1, 'screen': 2, 'evaluation': 3, 'smoke': 4}[phase] * 1000 + pi * 10


def expected_arms(phase):
    base = ['unsteered', *[f'random_{s}' for s in RANDOM]]
    if phase == 'tuning':
        return base + [f'{method}@{a}' for method in ['nla', 'diffmean'] for a in GRID]
    return base + ['nla', 'diffmean'] + (['prompting'] if phase == 'evaluation' else [])


def score_rows(rows, terms):
    from experiments.confirmation_metrics import evaluate_prompt, aggregate
    scored = [evaluate_prompt({'prompt_index':r['prompt_index'], 'generation_arms':r['generation_arms']}, terms) for r in rows]
    metrics = {}
    for arm in rows[0]['generation_arms']:
        samples = [s for r in scored for s in r['arms'][arm]]
        a = aggregate(samples)
        a['prompt_coverage'] = sum(any(s['concept_hit'] for s in r['arms'][arm]) for r in scored) / len(scored)
        a['quality_adjusted_hit_rate'] = sum(s['concept_hit'] and s['quality_pass'] for s in samples) / len(samples)
        a['quality_eligible'] = (a['quality_pass_rate'] >= .75 and a['mean_reference_similarity'] >= .12
                                 and a['mean_repeated_trigram_rate'] <= .1 and a['mean_target_term_count'] <= 4)
        metrics[arm] = a
    strongest = max(metrics[f'random_{s}']['hit_rate'] for s in RANDOM)
    for a in metrics.values():
        a['selection_score'] = min(a['hit_rate']-metrics['unsteered']['hit_rate'], a['hit_rate']-strongest) * a['prompt_coverage'] * a['quality_pass_rate']
    return scored, metrics


def choose_strengths(metrics):
    selected = {}
    for method in ['nla', 'diffmean']:
        eligible = [a for a in GRID if metrics[f'{method}@{a}']['quality_eligible']]
        candidates = eligible or GRID
        # A frozen symmetric fallback preserves all 100 concepts, even when quality fails.
        key = (lambda a: (metrics[f'{method}@{a}']['selection_score'], -a)) if eligible else (lambda a: (metrics[f'{method}@{a}']['quality_pass_rate'], metrics[f'{method}@{a}']['selection_score'], -a))
        selected[method] = {'alpha':max(candidates,key=key), 'quality_eligible':bool(eligible),
                            'rule':'quality-gated score maximization' if eligible else 'fallback: quality-pass rate then score'}
    return selected


def task_path(phase, cid, pi):
    return OUT / 'generations' / phase / f'c{cid}.p{pi}.json'


def validate_batch(path, cid, phase, pi, prompt, protocol_sha):
    row = read(path)
    assert row['concept_id'] == cid and row['phase'] == phase and row['prompt_index'] == pi
    assert row['prompt'] == prompt and row['protocol_sha256'] == protocol_sha
    assert row['seed'] == seed(cid, phase, pi)
    assert list(row['generation_arms']) == expected_arms(phase)
    assert all(len(v) == 2 and all(isinstance(x,str) for x in v) for v in row['generation_arms'].values())
    assert row['target_revision'] == read(OUT/'protocol.json')['models']['target']['revision']
    assert row['batch_integrity_sha256'] == signature({k:v for k,v in row.items() if k != 'batch_integrity_sha256'})
    return row


def phase_indices(phase):
    return list(range(*read(OUT/'protocol.json')['splits'][phase]))


def validate_shard(phase, shard, ids):
    protocol, pool, prompts, terms = load_inputs()
    hashes = {}; keys = set(); counts = 0
    for cid in ids:
        rows=[]
        for pi in phase_indices(phase):
            path=task_path(phase,cid,pi)
            row=validate_batch(path,cid,phase,pi,prompts[pi]['text'],sha(OUT/'protocol.json'))
            if phase in ['screen','evaluation']:
                tuning=read(OUT/'calibration'/f'c{cid}.json')
                assert row['selected_strengths_sha256']==sha(OUT/'calibration'/f'c{cid}.json')
                assert row['arm_metadata']['nla']['alpha']==tuning['selected']['nla']['alpha']
                assert row['arm_metadata']['diffmean']['alpha']==tuning['selected']['diffmean']['alpha']
            if phase=='evaluation':
                assert row['panels_sha256']==sha(OUT/'panels_frozen.json')
            for arm, responses in row['generation_arms'].items():
                for j in range(len(responses)):
                    key=(cid,pi,arm,j)
                    assert key not in keys
                    keys.add(key);counts+=1
            hashes[str(path.relative_to(ROOT))]=sha(path);rows.append(row)
        _, metrics=score_rows(rows,terms[str(cid)])
        directory='calibration' if phase=='tuning' else 'screen_metrics' if phase=='screen' else 'evaluation_metrics'
        result={'concept_id':cid,'metrics':metrics,'generation_hashes':{str(r['prompt_index']):sha(task_path(phase,cid,r['prompt_index'])) for r in rows}}
        if phase=='tuning': result['selected']=choose_strengths(metrics)
        result_path=OUT/directory/f'c{cid}.json'
        freeze(result_path,result)
        hashes[str(result_path.relative_to(ROOT))]=sha(result_path)
    receipt={'phase':phase,'shard':shard,'concept_ids':ids,'response_count':counts,'unique_response_keys':len(keys),'sha256':hashes,'validated':True}
    freeze(OUT/'validation'/f'{phase}.shard{shard}.json',receipt)
    return receipt
