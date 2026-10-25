"""Resumable panel-14 explanation-source ablation, one process per GPU."""
from pathlib import Path
import argparse
import fcntl
import json
import os
import sys
import time
import traceback

from audit_explanation_source_ablation import ROOT, digest, object_hash, save
import torch
from experiments import steering, vectors

OUT = ROOT / 'artifacts/experiments/nla_explanation_source_panel14'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--shard', type=int, choices=[0, 1], required=True)
    parser.add_argument('--fresh', action='store_true', help='Generate only A_fresh in seed_matched/')
    args = parser.parse_args()
    global OUT
    if args.fresh:
        OUT = OUT / 'seed_matched'
    # Advisory lock prevents two workers from writing the same shard on resume.
    lock = (OUT / f'worker{args.shard}.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((OUT / 'manifest.json').read_text())
    mapping = json.loads((OUT / 'source_target_mapping.json').read_text())
    assert digest(OUT / 'source_target_mapping.json') == manifest['mapping_sha256']
    ids = manifest['archived_protocol']['concept_ids'][args.shard::2]
    tasks = [m for m in mapping if m['condition'] in (('A_fresh',) if args.fresh else ('B', 'C')) and m['concept_id'] in ids]
    directory = OUT / 'batches'
    directory.mkdir(exist_ok=True)
    started = time.time()
    receipt_path = OUT / f'worker{args.shard}_runtime.json'
    previous_receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    attempts = previous_receipt.get('attempts', [])
    preserve_receipt = False
    receipt = {'shard': args.shard, 'concept_ids': ids, 'started_unix': started,
               'status': 'running', 'gpu': torch.cuda.get_device_name(0), 'attempts': attempts}
    def path_for(m):
        return directory / f'{m["condition"]}.c{m["concept_id"]}.p{m["target_prompt_index"]}.json'
    def verify(m, row):
        assert row['mapping_sha256'] == object_hash(m)
        assert row['seed'] == 42 + m['concept_id'] * 10000 + m['target_global_prompt_index'] * 10
        assert len(row['responses']) == 2 and row['condition'] == m['condition']
        assert row['target_revision'] == manifest['models']['target'][0]['revision']
        assert row['source_vector_sha256'] == m['archived_three_rollout_vector_sha256']
        assert row['generation_settings_sha256'] == object_hash(manifest['generation_settings'])
        assert row['concept_id'] == m['concept_id'] and row['prompt_index'] == m['target_prompt_index']
        assert row['global_prompt_index'] == m['target_global_prompt_index']
        assert row['alpha'] == m['alpha'] and row['prompt'] == m['target_prompt']
    try:
        pending = []
        for m in tasks:
            vector = ROOT / m['archived_three_rollout_vector']
            assert digest(vector) == m['archived_three_rollout_vector_sha256']
            direction = vectors.load(str(vector))
            assert torch.isfinite(direction).all() and abs(float(direction.norm()) - 1) < 1e-5
            if path_for(m).exists():
                verify(m, json.loads(path_for(m).read_text()))
            else:
                pending.append(m)
        if not pending and previous_receipt.get('status') == 'complete':
            preserve_receipt = True
            print(f'shard {args.shard}: all {len(tasks)} saved batches verified; no generation needed', flush=True)
            return
        save(receipt_path, receipt)
        if pending:
            model_path = ROOT / manifest['models']['target'][0]['local_path']
            torch.cuda.reset_peak_memory_stats()
            model, tokenizer = steering.load_target(str(model_path), 'cuda:0')
            for index, m in enumerate(pending):
                vector = ROOT / m['archived_three_rollout_vector']
                direction = vectors.load(str(vector))
                value = 42 + m['concept_id'] * 10000 + m['target_global_prompt_index'] * 10
                torch.manual_seed(value)
                torch.cuda.manual_seed_all(value)
                torch.cuda.synchronize()
                batch_start = time.monotonic()
                responses = steering.generate(model, tokenizer, [{'role': 'user', 'content': m['target_prompt']}],
                    20, direction, -1, m['alpha'], n_samples=2, max_new_tokens=96, temperature=0.7)
                torch.cuda.synchronize()
                row = {'condition': m['condition'], 'concept_id': m['concept_id'],
                    'prompt_index': m['target_prompt_index'], 'global_prompt_index': m['target_global_prompt_index'],
                    'prompt': m['target_prompt'], 'alpha': m['alpha'], 'seed': value,
                    'seed_scope': 'two-sample batched generation; reset identically for seed-matched conditions',
                    'mapping_sha256': object_hash(m), 'source_vector_sha256': digest(vector),
                    'generation_settings_sha256': object_hash(manifest['generation_settings']),
                    'target_revision': model_path.name, 'responses': responses,
                    'elapsed_seconds': time.monotonic() - batch_start,
                    'peak_allocated_bytes': torch.cuda.max_memory_allocated(),
                    'peak_reserved_bytes': torch.cuda.max_memory_reserved()}
                verify(m, row)
                save(path_for(m), row)
                print(f'shard {args.shard}: {index + 1}/{len(pending)} {m["condition"]} c{m["concept_id"]} p{m["target_prompt_index"]}', flush=True)
        receipt.update(status='complete', completed_batches=len(tasks), responses=len(tasks) * 2,
            finished_unix=time.time(), elapsed_seconds=time.time() - started,
            peak_allocated_bytes=torch.cuda.max_memory_allocated(), peak_reserved_bytes=torch.cuda.max_memory_reserved())
    except Exception:
        receipt.update(status='failed', error=traceback.format_exc(), finished_unix=time.time(), elapsed_seconds=time.time() - started)
        raise
    finally:
        if not preserve_receipt:
            receipt['attempts'].append({k: v for k, v in receipt.items() if k != 'attempts'})
            save(receipt_path, receipt)


if __name__ == '__main__':
    main()
