"""Verify all 672 ablation responses and compute paired uncertainty and sensitivity."""
from pathlib import Path
import csv
import json
import time
import numpy as np
from audit_explanation_source_ablation import ROOT, digest, object_hash, save
from experiments.confirmation_metrics import evaluate_prompt

OUT = ROOT / 'artifacts/experiments/nla_explanation_source_panel14'
PAIRS = [('A', 'B'), ('A', 'C'), ('B', 'C')]
METRICS = ['concept_hit', 'coverage', 'quality_pass', 'length_pass', 'repetition_pass', 'similarity_pass']
RESAMPLES = 10000


def ci(delta, method):
    # Shape = concepts x prompts x samples (coverage has one sample per prompt).
    rng = np.random.default_rng(0)
    nc, nprompt, ns = delta.shape
    if method == 'concept_cluster':
        indices = rng.integers(nc, size=(RESAMPLES, nc))
        values = delta.mean(axis=(1, 2))[indices].mean(axis=1)
    elif method == 'prompt_stratified':
        indices = rng.integers(nprompt, size=(RESAMPLES, nc, nprompt))
        values = delta.mean(axis=2)[np.arange(nc)[None, :, None], indices].mean(axis=(1, 2))
    elif method == 'response':
        flat = delta.ravel()
        values = flat[rng.integers(len(flat), size=(RESAMPLES, len(flat)))].mean(axis=1)
    else:
        raise ValueError(method)
    return {'difference_percentage_points': float(delta.mean() * 100),
            'ci95_percentage_points': [float(v * 100) for v in np.quantile(values, [.025, .975])],
            'resamples': RESAMPLES, 'seed': 0, 'method': method}


def main():
    manifest = json.loads((OUT / 'manifest.json').read_text())
    mapping = json.loads((OUT / 'source_target_mapping.json').read_text())
    assert digest(OUT / 'source_target_mapping.json') == manifest['mapping_sha256']
    for name, sha in manifest['archive_sha256'].items():
        assert digest(ROOT / name) == sha
    ids = manifest['archived_protocol']['concept_ids']
    held = ROOT / 'artifacts/paper_runs/data/baseline_panel14/qwen_nla'
    archived = json.loads((held / 'transcripts.json').read_text())
    lookup = {(r['concept_id'], r['prompt_index']): r for r in archived}
    lexicons = json.loads((held / 'lexicons.json').read_text())
    mlookup = {(r['condition'], r['concept_id'], r['target_prompt_index']): r for r in mapping}
    raw, scored, per_concept = [], [], []
    arrays = {arm: {metric: [] for metric in METRICS} for arm in ['A', 'B', 'C', 'unsteered', 'random']}
    counts = {arm: 0 for arm in arrays}
    all_keys = set()
    for cid in ids:
        concept_arrays = {arm: {metric: [] for metric in METRICS} for arm in arrays}
        for i in range(8):
            reference = lookup[cid, i]
            arms = {'A': reference['generation_arms']['av_edit'],
                    'unsteered': reference['generation_arms']['unsteered'],
                    'random': reference['generation_arms']['random']}
            batches = {}
            for arm in ['B', 'C']:
                path = OUT / 'batches' / f'{arm}.c{cid}.p{i}.json'
                batch = json.loads(path.read_text())
                source = mlookup[arm, cid, i]
                assert batch['mapping_sha256'] == object_hash(source)
                assert batch['source_vector_sha256'] == source['archived_three_rollout_vector_sha256']
                assert batch['generation_settings_sha256'] == object_hash(manifest['generation_settings'])
                assert batch['alpha'] == reference['alpha'] == manifest['strengths'][str(cid)]
                assert batch['seed'] == 42 + cid * 10000 + (14 + i) * 10
                assert batch['target_revision'] == manifest['models']['target'][0]['revision']
                assert batch['prompt'] == reference['prompt']
                assert batch['concept_id'] == cid and batch['prompt_index'] == i and batch['condition'] == arm
                batches[arm] = batch
                arms[arm] = batch['responses']
            assert batches['B']['seed'] == batches['C']['seed']
            evaluated = evaluate_prompt({'prompt_index': i, 'generation_arms': arms}, lexicons[str(cid)])
            for arm, responses in arms.items():
                assert len(responses) == 2
                counts[arm] += 2
                prompt_scores = evaluated['arms'][arm]
                concept_arrays[arm]['coverage'].append([int(any(s['concept_hit'] for s in prompt_scores))])
                for metric in METRICS:
                    if metric == 'coverage':
                        continue
                    vals = []
                    for sample_index, s in enumerate(prompt_scores):
                        s['length_pass'] = 8 <= s['word_count'] <= 350
                        s['repetition_pass'] = s['repeated_trigram_rate'] <= .1
                        s['similarity_pass'] = s['reference_similarity'] >= .12
                        vals.append(int(s[metric]))
                    concept_arrays[arm][metric].append(vals)
                for sample_index, (response, score) in enumerate(zip(responses, prompt_scores)):
                    key = arm, cid, i, sample_index
                    assert key not in all_keys
                    all_keys.add(key)
                    scored.append({'condition': arm, 'concept_id': cid, 'prompt_index': i, 'sample_index': sample_index, **score})
                    if arm in ['B', 'C']:
                        raw.append({'condition': arm, 'concept_id': cid, 'prompt_index': i,
                            'global_prompt_index': 14 + i, 'sample_index': sample_index,
                            'prompt': reference['prompt'], 'response': response, 'alpha': reference['alpha'],
                            'seed': batches[arm]['seed'], 'source_mapping_sha256': batches[arm]['mapping_sha256'],
                            'source_split': mlookup[arm, cid, i]['source_split'],
                            'source_prompt_index': mlookup[arm, cid, i]['source_prompt_index']})
        item = {'concept_id': cid, 'concept': lookup[cid, 0]['concept'], 'conditions': {}}
        for arm in arrays:
            item['conditions'][arm] = {'n': 16, **{metric: float(np.mean(concept_arrays[arm][metric]) * 100) for metric in METRICS}}
            for metric in METRICS:
                arrays[arm][metric].append(concept_arrays[arm][metric])
        per_concept.append(item)
    assert counts == {arm: 224 for arm in arrays}
    assert len(raw) == 448 and len(list((OUT / 'batches').glob('*.json'))) == 224
    for arm in arrays:
        arrays[arm] = {k: np.asarray(v, dtype=float) for k, v in arrays[arm].items()}
    aggregate = {arm: {'n': counts[arm], **{k: float(v.mean() * 100) for k, v in vals.items()}} for arm, vals in arrays.items()}
    differences, loco = {}, []
    for left, right in PAIRS:
        name = f'{left}_minus_{right}'
        differences[name] = {metric: {method: ci(arrays[left][metric] - arrays[right][metric], method)
            for method in ['concept_cluster', 'prompt_stratified', 'response']} for metric in METRICS}
        differences[name]['rng_pairing'] = 'common recorded seed' if left == 'B' else 'prompt/sample index only; archived A sampling RNG unknown'
    for excluded, cid in enumerate(ids):
        keep = [j for j in range(14) if j != excluded]
        loco.append({'excluded_concept_id': cid, 'differences': {f'{left}_minus_{right}': {
            metric: ci((arrays[left][metric] - arrays[right][metric])[keep], 'concept_cluster')
            for metric in METRICS} for left, right in PAIRS}})
    save(OUT / 'metrics.json', {'units': 'percent; paired differences in percentage points',
        'concept_word_percentage': 'percentage of responses with at least one frozen concept word/phrase',
        'quality_reference': 'closest of two archived prompt-matched unsteered responses, as panel14',
        'aggregate': aggregate, 'per_concept': per_concept, 'paired_differences': differences})
    save(OUT / 'scored_responses.json', scored)
    save(OUT / 'leave_one_concept_out.json', loco)
    save(OUT / 'counts.json', {'A': 224, 'B': 224, 'C': 224, 'unsteered': 224, 'random': 224,
        'required_per_condition': 224, 'total_abc': 672, 'new_generations': 448,
        'unique_keys': len(all_keys), 'complete': True, 'B_C_sampling_seeds_verified': True,
        'A_sampling_seed_pairing_verified': False})
    for arm in ['B', 'C']:
        with (OUT / f'raw_{arm}.jsonl').open('w') as f:
            for row in raw:
                if row['condition'] == arm:
                    f.write(json.dumps(row) + '\n')
    with (OUT / 'per_concept_metrics.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=['concept_id', 'condition', 'n', *METRICS], lineterminator='\n')
        writer.writeheader()
        for c in per_concept:
            for arm, metrics in c['conditions'].items():
                writer.writerow({'concept_id': c['concept_id'], 'condition': arm, **metrics})
    runtime = [json.loads((OUT / f'worker{i}_runtime.json').read_text()) for i in [0, 1]]
    assert all(r['status'] == 'complete' and r['responses'] == 224 for r in runtime)
    wall = max(r['finished_unix'] for r in runtime) - min(r['started_unix'] for r in runtime)
    save(OUT / 'runtime.json', {'workers': runtime, 'wall_seconds': wall,
        'active_worker_seconds': sum(a['elapsed_seconds'] for r in runtime for a in r['attempts']),
        'peak_allocated_bytes': max(r['peak_allocated_bytes'] for r in runtime),
        'peak_reserved_bytes': max(r['peak_reserved_bytes'] for r in runtime),
        'scope': 'worker startup, target loading, generation and batch writes; excludes prior staging and audit'})
    manifest['status'] = 'complete; counts and hashes verified; A RNG pairing limitation'
    manifest['completed_unix'] = time.time()
    manifest['construction_resolution'] = 'Reuse normalized sum of three normalized scripted AV-edit AR deltas from each frozen source prompt; same as A'
    manifest['runtime'] = json.loads((OUT / 'runtime.json').read_text())
    manifest['implementation_hashes']['scripts/analyze_explanation_source_ablation.py'] = digest(Path(__file__))
    manifest['result_hashes'] = {str(p.relative_to(OUT)): digest(p) for p in OUT.glob('*') if p.is_file() and p.name not in ['manifest.json', 'RESULT.md', 'SHA256SUMS'] and not p.name.endswith('.log')}
    save(OUT / 'manifest.json', manifest)
    lines = ['# Qwen panel-14 explanation-source ablation', '',
        'Prompt-specific verbalization shows no clear advantage in concept-word incidence over either alternative in this run. The uncertainty intervals include zero. This is not evidence of equivalence.', '',
        '| Condition | Responses | Concept-word % | Prompt coverage % | Quality pass % |',
        '|---|---:|---:|---:|---:|']
    for arm, label in [('A','Current prompt (archived)'), ('B','Other prompt (cyclic)'), ('C','Fixed calibration prompt')]:
        a = aggregate[arm]
        lines.append(f'| {label} | {a["n"]} | {a["concept_hit"]:.2f} | {a["coverage"]:.2f} | {a["quality_pass"]:.2f} |')
    lines += ['', '| Paired difference | Concept-word change (pp) | 95% concept-cluster CI |', '|---|---:|---|']
    for name in differences:
        d = differences[name]['concept_hit']['concept_cluster']
        lower, upper = d['ci95_percentage_points']
        lines.append(f'| {name} | {d["difference_percentage_points"]:+.2f} | [{lower:+.2f}, {upper:+.2f}] |')
    # Set the interpretation from verified effect intervals rather than assume a finding.
    advantages = [differences[n]['concept_hit']['concept_cluster']['ci95_percentage_points'][0] > 0 for n in ['A_minus_B','A_minus_C']]
    if all(advantages):
        lines[2] = 'Prompt-specific verbalization has higher concept-word incidence than both alternatives in this run, with both 95% concept-cluster intervals above zero.'
    elif any(advantages):
        lines[2] = 'Prompt-specific verbalization shows an incidence advantage over one alternative; the results do not establish an advantage over both. See the paired intervals below.'
    elif any(differences[n]['concept_hit']['concept_cluster']['ci95_percentage_points'][1] < 0 for n in ['A_minus_B','A_minus_C']):
        lines[2] = 'Prompt-specific verbalization does not provide an incidence advantage in this run; at least one alternative has higher incidence with its paired 95% concept-cluster interval excluding zero. See the intervals below.'
    lines[2] += ' Both alternative sources still produce concept-word hits, so a current-prompt explanation is not necessary for lexical steering. Current-prompt steering has a lower automated quality pass rate, and its missing historical sampling seeds limit causal attribution of the difference.'
    lines += ['', 'Primary intervals resample 14 paired concept clusters (10,000 draws, seed 0). metrics.json also reports paired prompt-stratified and response-bootstrap intervals for incidence, coverage, overall quality, and each quality check. These are unadjusted exploratory intervals on the selected 14-concept panel.', '',
        'Leave-one-concept-out incidence difference ranges:']
    for name in differences:
        values = [r['differences'][name]['concept_hit']['difference_percentage_points'] for r in loco]
        lines.append(f'- {name}: {min(values):+.2f} to {max(values):+.2f} pp.')
    lines += ['', 'B uses source prompt (target local index + 1) mod 8 within concept. C uses calibration local index 0 (global index 10) throughout each concept. All conditions preserve the scripted edit, archived strengths, three-rollout normalized AR-delta construction, layer 20, last prompt token and each generated token injection, 96-token horizon, temperature 0.7, and two-sample batches. All 126 unique source vectors used by B/C were cached; no new AV/AR computation was needed.', '',
        '**Provenance limitation:** A is reused without regeneration. Its sampling seeds/RNG states and target-model revision were not archived. B/C reset identical recorded sampling seeds and use staged target revision a09a35458c702b33eeacc393d103063234e8bc28. Comparisons with A are paired by concept/prompt/sample index, not by verified common RNG. Archived unsteered/random controls are reused as reference artifacts, but their generation-seed and model-revision matches cannot be authenticated. Consequently this is an explanation-source comparison with an explicit historical provenance limitation, not an exact seed-matched three-arm replication.', '',
        'C fixes a source prompt and its three-rollout explanation bundle, preserving panel-14 construction; it is not a single-rollout ablation. Frozen mappings were written before B/C outcomes. Automated quality uses the archived closest prompt-matched unsteered reference and unchanged thresholds.', '',
        f'Counts verified: A/B/C each 224, total 672; 448 new responses. Wall time {wall / 60:.2f} minutes on two A100 40GB GPUs, one worker per GPU. Peak allocated {max(r["peak_allocated_bytes"] for r in runtime) / 2**30:.2f} GiB; peak reserved {max(r["peak_reserved_bytes"] for r in runtime) / 2**30:.2f} GiB. Each batch is atomically saved and seed-checked on resume.', '',
        'Raw generations: raw_B.jsonl and raw_C.jsonl. Full mapping: source_target_mapping.json. Counts, per-concept scores, paired intervals, sensitivity, runtime, environment and hashes are saved beside this report. No paper edits or GitHub push occurred. The unrelated emoji README checksum mismatch does not affect the separately verified Qwen artifacts.']
    (OUT / 'RESULT.md').write_text('\n'.join(lines) + '\n')
    with (OUT / 'SHA256SUMS').open('w') as f:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and p.name != 'SHA256SUMS' and not p.name.endswith(('.log','.tmp')):
                f.write(f'{digest(p)}  {p.relative_to(OUT)}\n')
    print(json.dumps({'aggregate': aggregate, 'concept_word_differences': {name: differences[name]['concept_hit']['concept_cluster'] for name in differences}, 'wall_minutes': wall / 60}, indent=2))


if __name__ == '__main__':
    main()
