"""Freeze panel-14 explanation mappings and audit reference provenance; no generation."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src/nla'), str(ROOT / 'artifacts/cache/mirror_runtime')]
from experiments.confirmation_metrics import evaluate_prompt, aggregate


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def main():
    started = time.monotonic()
    out = ROOT / 'artifacts/experiments/nla_explanation_source_panel14'
    out.mkdir(parents=True, exist_ok=True)
    if any((out / 'batches').glob('*.json')):
        raise RuntimeError('Generation has begun; refusing to overwrite frozen mapping or completed results. Resume the worker scripts instead.')
    base = ROOT / 'artifacts/paper_runs/data/baseline_panel14'
    held, cal = base / 'qwen_nla', base / 'qwen_nla_calibration'
    protocol = json.loads((held / 'protocol.json').read_text())
    ids = protocol['concept_ids']
    assert len(ids) == 14 and len(set(ids)) == 14
    assert protocol['n_prompts'] == 8 and protocol['n_samples'] == 2
    rows = json.loads((held / 'transcripts.json').read_text())
    calibration = json.loads((cal / 'transcripts.json').read_text())
    lookup = {(r['concept_id'], r['prompt_index']): r for r in rows}
    clookup = {(r['concept_id'], r['prompt_index']): r for r in calibration}
    assert len(lookup) == len(rows) == 112
    alphas = json.loads((cal / 'selected_alphas.json').read_text())
    prompts = json.loads((held / 'prompts.json').read_text())
    lexicons = json.loads((held / 'lexicons.json').read_text())
    expected = {}
    for line in (ROOT / 'artifacts/paper_runs/paper-sha256sums.txt').read_text().splitlines():
        sha, name = line.split(maxsplit=1)
        expected[name] = sha
    hashes = {}
    for directory in (held, cal):
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                key = str(path.relative_to(ROOT))
                hashes[key] = digest(path)
                assert key in expected, f'unregistered archive: {key}'
                assert hashes[key] == expected[key], f'checksum mismatch: {key}'
    mapping = []
    metrics = []
    counts = {}
    fixed_sources = {}
    for cid in ids:
        fixed = clookup[cid, 0]
        fixed_sources[str(cid)] = {'local_prompt_index': 0, 'global_prompt_index': 10,
                                   'prompt': fixed['prompt'], 'rollouts_sha256': object_hash(fixed['rollouts'])}
        evaluated = []
        for i in range(8):
            row = lookup[cid, i]
            assert row['status'] == 'complete'
            assert row['prompt'] == prompts[str(cid)][i]
            assert row['alpha'] == alphas[str(cid)]
            assert len(row['rollouts']) == 3
            evaluated.append(evaluate_prompt(row, lexicons[str(cid)]))
            for arm, responses in row['generation_arms'].items():
                assert len(responses) == 2
                counts[arm] = counts.get(arm, 0) + len(responses)
            for condition, directory, source in [('A', held, row), ('B', held, lookup[cid, (i + 1) % 8]), ('C', cal, fixed)]:
                source_index = source['prompt_index']
                vector = directory / 'vectors' / f'c{cid}.p{source_index}.av_edit.pt'
                assert vector.is_file()
                addition = f'\n\nThe representation specifically concerns: {row["concept"]}.\n'
                for rollout in source['rollouts']:
                    original = rollout['original_explanation']
                    edited = original.replace('</explanation>', addition + '</explanation>', 1) if '</explanation>' in original else original + addition
                    assert edited == rollout['edited_explanation']
                mapping.append({'condition': condition, 'concept_id': cid, 'concept': row['concept'],
                    'target_prompt_index': i, 'target_global_prompt_index': 14 + i,
                    'target_prompt': row['prompt'], 'target_prompt_sha256': object_hash(row['prompt']),
                    'source_split': 'calibration' if condition == 'C' else 'held_out',
                    'source_prompt_index': source_index,
                    'source_global_prompt_index': (10 if condition == 'C' else 14) + source_index,
                    'source_prompt': source['prompt'], 'source_prompt_sha256': object_hash(source['prompt']),
                    'source_rollouts': source['rollouts'], 'source_rollouts_sha256': object_hash(source['rollouts']),
                    'archived_three_rollout_vector': str(vector.relative_to(ROOT)),
                    'archived_three_rollout_vector_sha256': digest(vector), 'alpha': row['alpha'],
                    'construction_status': 'A archived; B/C pending clarification of single explanation versus three rollouts'})
        arms = {}
        for arm in ('av_edit', 'unsteered', 'random'):
            samples = [s for p in evaluated for s in p['arms'][arm]]
            arms[arm] = aggregate(samples) | {'prompt_coverage': sum(any(s['concept_hit'] for s in p['arms'][arm]) for p in evaluated) / 8}
        metrics.append({'concept_id': cid, 'arms': arms, 'prompt_metrics': evaluated})
    assert all(n == 224 for n in counts.values())
    save(out / 'source_target_mapping.json', mapping)
    save(out / 'reference_metrics.json', {'concepts': metrics, 'aggregate': {arm: aggregate([s for c in metrics for p in c['prompt_metrics'] for s in p['arms'][arm]]) | {'prompt_coverage': sum(any(s['concept_hit'] for s in p['arms'][arm]) for c in metrics for p in c['prompt_metrics']) / 112} for arm in ('av_edit', 'unsteered', 'random')}})
    save(out / 'counts.json', {'A': counts['av_edit'], 'B': 0, 'C': 0, 'archived_arms': counts, 'required_per_condition': 224, 'complete': False})
    gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used', '--format=csv'], text=True)
    models = {}
    for role, name in [('target', 'Qwen--Qwen2.5-7B-Instruct'), ('av', 'kitft--nla-qwen2.5-7b-L20-av'), ('ar', 'kitft--nla-qwen2.5-7b-L20-ar')]:
        snapshots = ROOT / 'artifacts/models/hub' / ('models--' + name) / 'snapshots'
        models[role] = [{'local_path': str(p.relative_to(ROOT)), 'revision': p.name,
                        'config_sha256': digest(p / 'config.json')} for p in snapshots.iterdir() if (p / 'config.json').is_file()]
    manifest = {'status': 'awaiting_protocol_clarification; no new generation',
        'repository_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'sources_lock': (ROOT / 'sources.lock').read_text(), 'archived_protocol': protocol,
        'mapping_rule': 'B source local index = (target local index + 1) mod 8; C source calibration local index = 0 (global 10)',
        'fixed_sources': fixed_sources, 'strengths': alphas,
        'mapping_sha256': digest(out / 'source_target_mapping.json'), 'archive_sha256': hashes,
        'models': models, 'archived_target_revision': None,
        'archived_generation_seeds': None, 'archived_generation_rng_states': None,
        'random_control_seeds_are_vector_seeds': [17, 42, 101],
        'control_reuse_status': 'artifact integrity verified; generation seed and target revision match cannot be authenticated',
        'environment': {'python': sys.version, 'platform': platform.platform(), 'gpu': gpu,
            'installed_packages': sorted(f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions())},
        'implementation_hashes': {name: digest(ROOT / name) for name in ['scripts/audit_explanation_source_ablation.py', 'src/nla/experiments/discovery500.py', 'src/nla/experiments/steering.py', 'src/nla/experiments/confirmation_metrics.py', 'artifacts/cache/mirror_runtime/nla_inference.py']},
        'generation_settings': {'layer': 20, 'position': -1, 'n_samples': 2, 'max_new_tokens': 96, 'temperature': 0.7, 'dtype': 'bfloat16'},
        'runtime': {'audit_elapsed_seconds': time.monotonic() - started, 'new_gpu_generation_seconds': 0, 'generation_peak_memory_bytes': None},
        'unrelated_full_archive_check_failure': 'artifacts/paper_runs/data/freeform_emoji/README.md checksum mismatch'}
    save(out / 'manifest.json', manifest)
    (out / 'RESULT.md').write_text('# Explanation-source ablation: preparation only\n\nNo conclusion about prompt-specific verbalization is available: B and C have not been generated.\n\nQwen held-out and calibration archive hashes pass. A contains exactly 224 responses; B and C contain zero. All archived unsteered and random arms contain 224 responses each. The 336-row A/B/C source-target mapping is frozen: cyclic next held-out prompt for B, first calibration prompt (global index 10) for C. Strengths and word lists are unchanged.\n\nExecution needs protocol clarification: the archived target generations have no sampling seed or RNG-state receipts. Seeds 17,42,101 are random-vector seeds, not target-generation seeds. Exact sampling pairing with archived A cannot be verified. The archived target model revision is also absent from its protocol; the staged revision is recorded separately.\n\nPanel-14 averages three normalized explanation deltas per source prompt; using a single explanation changes construction. The mapping records all three archived rollouts while the requested construction is clarified.\n\nNo new generation, paired confidence intervals, or leave-one-concept-out effects are reported. No paper edits or GitHub push occurred.\n')
    print(f'A and controls: 224 each; archive hashes valid; {len(mapping)} mapping rows -> {out}')


if __name__ == '__main__':
    main()
