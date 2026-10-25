"""Generate the wordle-2 sweep specs + batch script. Run POD-SIDE
(python emits clean LF there; Windows-generated shell scripts carried
CRLF and broke bash once already).

    python experiments/wordle2/gen_specs.py

Writes specs/ (the final gated arms from README.md) and run_wordle.sh. Skips any
vector missing from data/wordle_vectors.npz (e.g. a gated edit whose
substrate check failed) and says so loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
PROMPTS = ["Think of a word to start a wordle game.",
           "Give me a starting word for wordle.",
           "What's a good wordle opener?",
           "Suggest a wordle starting word."]
ALPHAS = [0, 0.2, 0.4, 0.6, 0.8]
LENGTH_PROBES = [
    "answer_letters", "wordle_any_valid", "wordle_crane", "n_words",
    "unique_word_ratio",
]
MEANING_PROBES = [
    "wordle_crane", "answer_letters", "wordle_any_valid", "n_words",
    "unique_word_ratio",
]
# Final sweep list after the two screening rounds (2026-08-14 night).
# Gated out with reasons in wordle_build_report.json: all verbalize-edit
# length arms (substrate lacks "five"), comp25_bestlen (selection rule
# found no number-word weight in any candidate), anchor_five(+_b) and
# mix_poor(+_b) (anchor readouts pure junk; composition's own gate
# requires both parents to pass). ff_a_six kept deliberately as the
# pre-registered forecast-negative; poor_lion_b kept as a forecast-test
# (readout predicts royal-register, NOT lion).
ARMS = [  # (spec name, npz key or None for random/census, probes)
    ("census", None, MEANING_PROBES),
    ("random", "RANDOM", MEANING_PROBES),
    ("len_four_b", "len_four_b", LENGTH_PROBES),
    ("len_six_b", "len_six_b", LENGTH_PROBES),
    ("len_seven_b", "len_seven_b", LENGTH_PROBES),
    ("len_digit_six_b", "len_digit_six_b", LENGTH_PROBES),
    ("ff_a_six", "ff_a_six", LENGTH_PROBES),
    ("ff_dwell", "ff_dwell", MEANING_PROBES),
    ("comp25_dwell", "comp25_ff_dwell", MEANING_PROBES),
    ("resid25_dwell", "resid25_ff_dwell", MEANING_PROBES),
    ("poor_lion_b", "poor_lion_b", MEANING_PROBES),
]

# Every whole vector that survived the build gates and entered the behavioral
# sweep was built from two researcher-authored descriptions under the same
# ``Reply gives {...}`` scaffold. The canonical AV-edit length candidates did
# not pass their gates and are intentionally absent from this mapping.
AR_CONTRAST_KEYS = {
    "len_four_b", "len_six_b", "len_seven_b", "len_digit_six_b",
    "ff_a_six", "ff_dwell", "poor_lion_b",
}
DERIVED_KEYS = {
    "comp25_ff_dwell": ("ff_dwell", "component"),
    "resid25_ff_dwell": ("ff_dwell", "remainder"),
}


def _none_root() -> dict[str, str]:
    return {
        "method": "none",
        "text_origin": "none",
        "contrast_relation": "none",
        "edit_granularity": "none",
        "pairing_style": "not_applicable",
    }


def _random_root() -> dict[str, str]:
    return {
        "method": "random_isotropic",
        "text_origin": "not_applicable",
        "contrast_relation": "not_applicable",
        "edit_granularity": "not_applicable",
        "pairing_style": "not_applicable",
    }


def _authored_ar_root() -> dict[str, str]:
    return {
        "method": "nla_ar_contrast",
        "text_origin": "authored",
        "contrast_relation": "paired_descriptions",
        "edit_granularity": "not_applicable",
        "pairing_style": "matched_scaffold",
    }


def _greedy_k25() -> dict[str, Any]:
    return {
        "method": "reference_inspired_greedy_nnls",
        "dictionary": "full_vocabulary_j_lens_atoms",
        "candidate_pool": "full_vocabulary",
        "atom_normalization": "unit_l2",
        "solver": "greedy_max_positive_correlation_with_nnls_refit",
        "paper_solver_relation": "reference_inspired_divergence",
        "nonnegative": True,
        "k_requested": 25,
    }


def method_for(key: str | None) -> dict[str, Any]:
    """Return explicit steering-taxonomy/v1 ancestry for a generated arm."""
    if key is None:
        root = _none_root()
        derivation = {"operator": "none", "output_role": "none"}
        vector_view = "none"
    elif key == "RANDOM":
        root = _random_root()
        derivation = {"operator": "none", "output_role": "whole"}
        vector_view = "unit_l2"
    elif key in AR_CONTRAST_KEYS:
        root = _authored_ar_root()
        derivation = {"operator": "none", "output_role": "whole"}
        vector_view = "unit_l2"
    elif key in DERIVED_KEYS:
        parent, role = DERIVED_KEYS[key]
        root = _authored_ar_root()
        derivation = {
            "operator": "jspace_sparse_nonnegative",
            "output_role": role,
            "parent_vector_key": parent,
            "parameters": _greedy_k25(),
        }
        vector_view = "unit_l2"
    else:
        raise ValueError(f"no reviewed method provenance for Wordle2 key {key!r}")
    return {
        "schema": "steering-taxonomy/v1",
        "root": root,
        "derivation": derivation,
        "vector_view": vector_view,
    }


def design_for(key: str | None) -> dict[str, Any]:
    role = (
        "baseline_reference" if key is None else
        "negative_control" if key == "RANDOM" else
        "treatment"
    )
    return {
        "schema": "steering-study/v1",
        "intent": "confirmatory",
        "preregistered": True,
        "arm_role": role,
        "notes": ["Final behavioral sweep preregistered in wordle2/README.md."],
    }


def build_spec(name: str, key: str | None, probes: list[str]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": f"wordle2-{name.replace('_', '-')}",
        "matrix_id": "T3-wordle-word",
    }
    if key == "RANDOM":
        spec.update(direction="random", direction_seed=0, alphas=ALPHAS)
    elif key is not None:
        spec.update(
            direction_file=f"experiments/wordle2/data/wordle_vectors.npz#{key}",
            alphas=ALPHAS,
        )
    spec.update(
        method=method_for(key),
        design=design_for(key),
        prompts=PROMPTS,
        mode="all",
        samples_per_prompt=15,
        temperature=0.7,
        max_new_tokens=28,
        probes=probes,
    )
    return spec


def main() -> None:
    specs = HERE / "specs"
    specs.mkdir(exist_ok=True)
    available = set(np.load(HERE / "data" / "wordle_vectors.npz").files)
    order = []
    for name, key, probes in ARMS:
        if key not in {None, "RANDOM"} and key not in available:
            print(f"SKIP {name}: key {key} not in wordle_vectors.npz")
            continue
        spec = build_spec(name, key, probes)
        (specs / f"{name}.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        order.append(name)
    (HERE / "run_wordle.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nfor s in " + " ".join(order)
        + "; do\n    echo \"==== $s ====\"\n"
        "    python experiments/run.py \"experiments/wordle2/specs/$s.yaml\"\ndone\n"
        "echo \"wordle2 batch complete\"\n", encoding="utf-8")
    print(f"wrote {len(order)} specs + run_wordle.sh: {', '.join(order)}")


if __name__ == "__main__":
    main()
