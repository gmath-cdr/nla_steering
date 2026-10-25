"""Generate the Gemma confirmatory-core specs + batch script. RUN POD-SIDE
(Windows-generated scripts carry CRLF; this lesson is now doctrine).

    python experiments/gemma_core/gen_specs_gemma.py

Alphas use Gemma's own measured window (smoke ladder: coherent to 0.15,
flood at 0.2): [0, 0.05, 0.1, 0.15, 0.2]. Emoji arms use the chat
prompts; wordle arms the wordle prompts. Skips missing npz keys loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
ALPHAS = [0, 0.05, 0.1, 0.15, 0.2]
CHAT_PROMPTS = ["Tell me about your day.", "What should I cook for dinner tonight?",
                "Describe the weather in autumn.",
                "Give me tips for staying focused while studying.",
                "Recommend a weekend activity."]
WORDLE_PROMPTS = ["Think of a word to start a wordle game.",
                  "Give me a starting word for wordle.",
                  "What's a good wordle opener?",
                  "Suggest a wordle starting word."]
EMOJI_PROBES = ["emojis", "exclamations", "n_words", "unique_word_ratio"]
WORDLE_PROBES = [
    "wordle_crane", "answer_letters", "wordle_any_valid", "n_words",
    "unique_word_ratio",
]
LEN_PROBES = [
    "answer_letters", "wordle_any_valid", "wordle_crane", "n_words",
    "unique_word_ratio",
]
ARMS = [
    ("census_wordle", None, WORDLE_PROMPTS, WORDLE_PROBES),
    ("random_chat", "RANDOM", CHAT_PROMPTS, EMOJI_PROBES),
    ("random_wordle", "RANDOM", WORDLE_PROMPTS, WORDLE_PROBES),
    ("g_freeform", "g_freeform", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_emoji", "g_emoji", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_emojis", "g_emojis", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_emoticon", "g_emoticon", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_smiley", "g_smiley", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_glyphword", "g_glyphword", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_phrase_packed", "g_phrase_packed", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_phrase_glyph", "g_phrase_glyph", CHAT_PROMPTS, EMOJI_PROBES),
    ("comp25_g_freeform", "comp25_g_freeform", CHAT_PROMPTS, EMOJI_PROBES),
    ("resid25_g_freeform", "resid25_g_freeform", CHAT_PROMPTS, EMOJI_PROBES),
    ("comp25_g_phrase_glyph", "comp25_g_phrase_glyph", CHAT_PROMPTS, EMOJI_PROBES),
    ("resid25_g_phrase_glyph", "resid25_g_phrase_glyph", CHAT_PROMPTS, EMOJI_PROBES),
    ("g_dwell", "g_dwell", WORDLE_PROMPTS, WORDLE_PROBES),
    ("comp25_g_dwell", "comp25_g_dwell", WORDLE_PROMPTS, WORDLE_PROBES),
    ("resid25_g_dwell", "resid25_g_dwell", WORDLE_PROMPTS, WORDLE_PROBES),
    ("g_lion", "g_lion", WORDLE_PROMPTS, WORDLE_PROBES),
    ("g_len_seven", "g_len_seven", WORDLE_PROMPTS, LEN_PROBES),
]

AR_CONTRAST_KEYS = {
    "g_freeform": "unconstrained",
    "g_dwell": "matched_scaffold",
    "g_lion": "matched_scaffold",
    "g_len_seven": "matched_scaffold",
}
AV_SINGLE_KEYS = {"g_emoji", "g_emojis", "g_emoticon", "g_smiley", "g_glyphword"}
AV_PHRASE_KEYS = {"g_phrase_packed", "g_phrase_glyph"}
DERIVED_KEYS = {
    "comp25_g_freeform": ("g_freeform", "component"),
    "resid25_g_freeform": ("g_freeform", "remainder"),
    "comp25_g_phrase_glyph": ("g_phrase_glyph", "component"),
    "resid25_g_phrase_glyph": ("g_phrase_glyph", "remainder"),
    "comp25_g_dwell": ("g_dwell", "component"),
    "resid25_g_dwell": ("g_dwell", "remainder"),
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


def _ar_root(pairing_style: str) -> dict[str, str]:
    return {
        "method": "nla_ar_contrast",
        "text_origin": "authored",
        "contrast_relation": "paired_descriptions",
        "edit_granularity": "not_applicable",
        "pairing_style": pairing_style,
    }


def _av_edit_root(granularity: str) -> dict[str, str]:
    return {
        "method": "nla_av_edit_ar",
        "text_origin": "av_generated_substrate",
        "contrast_relation": "edited_vs_original",
        "edit_granularity": granularity,
        "pairing_style": "not_applicable",
    }


def _whole_root(key: str) -> dict[str, str]:
    if key in AR_CONTRAST_KEYS:
        return _ar_root(AR_CONTRAST_KEYS[key])
    if key in AV_SINGLE_KEYS:
        return _av_edit_root("single_site")
    if key in AV_PHRASE_KEYS:
        return _av_edit_root("phrase")
    raise ValueError(f"no reviewed Gemma parent provenance for {key!r}")


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
    elif key in DERIVED_KEYS:
        parent, role = DERIVED_KEYS[key]
        root = _whole_root(parent)
        derivation = {
            "operator": "jspace_sparse_nonnegative",
            "output_role": role,
            "parent_vector_key": parent,
            "parameters": _greedy_k25(),
        }
        vector_view = "unit_l2"
    else:
        root = _whole_root(key)
        derivation = {"operator": "none", "output_role": "whole"}
        vector_view = "unit_l2"
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
        "notes": ["Cross-model confirmatory sweep preregistered in gemma_core/README.md."],
    }


def build_spec(
    name: str,
    key: str | None,
    prompts: list[str],
    probes: list[str],
) -> dict[str, Any]:
    is_wordle = prompts is WORDLE_PROMPTS
    spec: dict[str, Any] = {
        "name": f"gcore-{name.replace('_', '-')}",
        "matrix_id": "GEMMA-core",
    }
    if key == "RANDOM":
        spec.update(direction="random", direction_seed=0, alphas=ALPHAS)
    elif key is not None:
        spec.update(
            direction_file=f"experiments/gemma_core/data/gemma_vectors.npz#{key}",
            alphas=ALPHAS,
        )
    spec.update(
        method=method_for(key),
        design=design_for(key),
        prompts=prompts,
        mode="all",
        samples_per_prompt=15,
        temperature=0.7,
        max_new_tokens=28 if is_wordle else 60,
        probes=probes,
    )
    return spec


def main() -> None:
    specs = HERE / "specs"
    specs.mkdir(exist_ok=True)
    available = set(np.load(HERE / "data" / "gemma_vectors.npz").files)
    order = []
    for name, key, prompts, probes in ARMS:
        if key not in {None, "RANDOM"} and key not in available:
            print(f"SKIP {name}: {key} not in gemma_vectors.npz")
            continue
        spec = build_spec(name, key, prompts, probes)
        (specs / f"{name}.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        order.append(name)
    (HERE / "run_gemma_core.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nfor s in " + " ".join(order)
        + "; do\n    echo \"==== $s ====\"\n"
        "    python experiments/run.py \"experiments/gemma_core/specs/$s.yaml\"\ndone\n"
        "echo \"gemma core batch complete\"\n", encoding="utf-8")
    print(f"wrote {len(order)} specs + run_gemma_core.sh")


if __name__ == "__main__":
    main()
