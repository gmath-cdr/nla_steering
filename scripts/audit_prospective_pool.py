"""Audit whether a never-screened Concept500 pool can be sampled prospectively.

No model loading or generation. Existing study artifacts are read only.
"""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit():
    metadata = ROOT / 'src/axbench/axbench/concept500/prod_9b_l20_v1/generate/metadata.jsonl'
    universe = {int(row['concept_id']) for row in map(json.loads, metadata.read_text().splitlines())
                if row.get('concept_id') is not None}
    assert len(universe) == 500
    studies = []
    excluded = set()
    for name in ['av_edit_discovery500_2026-08-11', 'diffmean_discovery500_2026-08-15_merged']:
        source = ROOT / 'artifacts/paper_runs/data/qwen2.5-7b' / name / 'summary.json'
        rows = json.loads(source.read_text())['concepts']
        ids = {int(row['concept_id']) for row in rows}
        assert len(ids) == len(rows) == 500
        excluded.update(ids)
        studies.append({'path': str(source.relative_to(ROOT)), 'sha256': digest(source),
                        'concept_count': len(ids), 'concept_ids': sorted(ids),
                        'shortlisted_candidate_ids': sorted(int(r['concept_id']) for r in rows if r['discovery_candidate'])})
    eligible = sorted(universe - excluded)
    return {'status': 'infeasible_under_literal_exclusion' if len(eligible) < 100 else 'eligible',
            'requested_pool_size': 100, 'universe_size': len(universe),
            'metadata_path': str(metadata.relative_to(ROOT)), 'metadata_sha256': digest(metadata),
            'exclusion_scope': 'Every concept screened in earlier selection experiments; these two studies alone exhaust Concept500.',
            'prior_selection_studies': studies, 'eligible_concept_ids': eligible,
            'eligible_count': len(eligible), 'sampled_concept_ids': [],
            'generation_started': False,
            'required_clarification': 'Permit previously screened concepts while excluding prior selected/shortlisted concepts, or specify a new concept corpus.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit()
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids replacing any prior prospective audit.
        with args.output.open('x') as stream:
            stream.write(text)
    print(json.dumps({k: result[k] for k in ['status', 'universe_size', 'eligible_count', 'generation_started']}))


if __name__ == '__main__':
    main()
