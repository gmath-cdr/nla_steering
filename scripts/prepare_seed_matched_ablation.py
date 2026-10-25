"""Freeze A_fresh provenance without modifying the historical experiment."""
import importlib.metadata
import json
import subprocess
import sys
import time
from audit_explanation_source_ablation import ROOT, digest, object_hash, save

BASE = ROOT / 'artifacts/experiments/nla_explanation_source_panel14'
OUT = BASE / 'seed_matched'


def main():
    if (OUT / 'manifest.json').exists():
        print('Frozen manifest exists; use --fresh workers to resume.')
        return
    original = json.loads((BASE / 'manifest.json').read_text())
    assert digest(BASE / 'source_target_mapping.json') == original['mapping_sha256']
    for name, sha in original['archive_sha256'].items():
        assert digest(ROOT / name) == sha, name
    # Authenticate all historical outputs before freezing the new comparison.
    historical = {}
    for line in (BASE / 'SHA256SUMS').read_text().splitlines():
        sha, name = line.split(maxsplit=1)
        assert digest(BASE / name) == sha, name
        historical[name] = sha
    mapping = []
    for row in json.loads((BASE / 'source_target_mapping.json').read_text()):
        if row['condition'] == 'A':
            row = dict(row, condition='A_fresh', construction_status='Current target prompt cached three-rollout explanation/AR direction')
            assert row['source_global_prompt_index'] == row['target_global_prompt_index']
            assert digest(ROOT / row['archived_three_rollout_vector']) == row['archived_three_rollout_vector_sha256']
            mapping.append(row)
    assert len(mapping) == 112
    OUT.mkdir(exist_ok=True)
    save(OUT / 'source_target_mapping.json', mapping)
    manifest = {k: original[k] for k in ['models', 'generation_settings', 'effective_target_generation_config', 'archived_protocol', 'strengths']}
    manifest.update(status='frozen before A_fresh generation', frozen_unix=time.time(),
        mapping_sha256=digest(OUT / 'source_target_mapping.json'),
        historical_manifest_sha256=digest(BASE / 'manifest.json'), historical_sha256=historical,
        seed_rule='42 + concept_id * 10000 + target_global_prompt_index * 10',
        construction='Reuse current target prompt three-rollout explanations and normalized reconstructed AR direction; no new AV/AR calls',
        repository_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        environment={'python':sys.version, 'packages':{n:importlib.metadata.version(n) for n in ['torch','transformers','numpy','tokenizers','safetensors','accelerate']},
                     'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=index,name,uuid','--format=csv'],text=True)},
        historical_environment=original['environment'],
        limitation='Common recorded batch seeds, model revision and generation parameters; software versions and A100 hardware variant differ from historical B/C, so bitwise RNG equivalence is not guaranteed.',
        implementation_hashes={n:digest(ROOT/n) for n in ['scripts/run_explanation_source_ablation.py','scripts/prepare_seed_matched_ablation.py','src/nla/experiments/steering.py','src/nla/experiments/confirmation_metrics.py']})
    model = ROOT / manifest['models']['target'][0]['local_path']
    assert digest(model / 'config.json') == manifest['models']['target'][0]['config_sha256']
    # Check full staged target weights against the historical checkpoint receipt.
    runtime = ROOT / 'artifacts/cache/mirror_runtime/nla_inference.py'
    assert digest(runtime) == original['implementation_hashes']['artifacts/cache/mirror_runtime/nla_inference.py']
    manifest['inference_runtime_sha256'] = digest(runtime)
    checkpoints=json.loads((BASE/'checkpoint_hashes.json').read_text())
    for name, info in checkpoints['target']['files'].items():
        assert digest(model / name) == info['sha256'], name
    manifest['target_checkpoint_files_verified'] = checkpoints['target']
    save(OUT / 'manifest.json', manifest)
    print(f'Frozen {len(mapping)} tasks -> {OUT}')

if __name__ == '__main__':
    main()
