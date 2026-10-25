from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "steering_taxonomy_tested", LAB / "nla_lab" / "methods.py"
)
assert SPEC is not None and SPEC.loader is not None
METHODS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METHODS)


class MethodTaxonomyTests(unittest.TestCase):
    def test_inline_pair_is_ar_contrast_not_av_edit(self) -> None:
        spec = {
            "name": "inline",
            "direction": {"positive": "house", "negative": "crane"},
            "prompts": ["word"],
            "mode": "all",
        }
        method = METHODS.resolve_method(spec)
        self.assertEqual(method["root"]["method"], "nla_ar_contrast")
        self.assertEqual(method["derivation"]["output_role"], "whole")
        self.assertEqual(method["intervention"]["token_scope"], "prompt_and_decode_all")

    def test_census_and_random_are_distinct_study_roles(self) -> None:
        census_spec = {"name": "census", "prompts": ["x"]}
        census = METHODS.resolve_method(census_spec)
        census_design = METHODS.resolve_design(census_spec, census)
        self.assertEqual(census["root"]["method"], "none")
        self.assertEqual(census_design["arm_role"], "baseline_reference")

        random_spec = {"name": "random", "direction": "random", "prompts": ["x"]}
        random = METHODS.resolve_method(random_spec)
        random_design = METHODS.resolve_design(random_spec, random)
        self.assertEqual(random["root"]["method"], "random_isotropic")
        self.assertEqual(random_design["arm_role"], "negative_control")

    def test_emoji_edit_and_derived_parent_ancestry(self) -> None:
        whole_spec = {
            "name": "emoji-edit",
            "direction_file": "experiments/emoji_methods/data/vectors.npz#single_1",
            "prompts": ["x"],
        }
        whole = METHODS.resolve_method(whole_spec)
        self.assertEqual(whole["root"]["method"], "nla_av_edit_ar")
        self.assertEqual(whole["root"]["edit_granularity"], "single_site")

        derived_spec = {
            "name": "emoji-component",
            "direction_file": (
                "experiments/emoji_methods/data/decomposition_v2.npz#comp25_single_1"
            ),
            "prompts": ["x"],
        }
        derived = METHODS.resolve_method(derived_spec)
        self.assertEqual(derived["root"]["method"], "nla_av_edit_ar")
        self.assertEqual(derived["derivation"]["operator"], "jspace_sparse_nonnegative")
        self.assertEqual(derived["derivation"]["output_role"], "component")
        self.assertEqual(derived["derivation"]["parent_vector_key"], "single_1")

    def test_legacy_ols_split_is_not_k25(self) -> None:
        spec = {
            "name": "legacy-readable",
            "direction_file": "experiments/emoji_methods/data/vectors.npz#readable",
            "prompts": ["x"],
        }
        method = METHODS.resolve_method(spec)
        self.assertEqual(method["derivation"]["operator"], "j_readout_ols_split")
        self.assertEqual(method["derivation"]["output_role"], "component")

    def test_unknown_file_key_fails_closed(self) -> None:
        spec = {
            "name": "unknown",
            "direction_file": "experiments/emoji_methods/data/vectors.npz#mystery",
            "prompts": ["x"],
        }
        method = METHODS.resolve_method(spec)
        self.assertEqual(method["root"]["method"], "unknown")
        self.assertEqual(method["classification"]["confidence"], "low")

    def test_explicit_direction_mismatch_is_rejected(self) -> None:
        spec = {
            "name": "bad",
            "direction": {"positive": "a", "negative": "b"},
            "prompts": ["x"],
            "method": {
                "schema": METHODS.TAXONOMY_ID,
                "root": {
                    "method": "nla_av_edit_ar",
                    "text_origin": "av_generated_substrate",
                    "contrast_relation": "edited_vs_original",
                    "edit_granularity": "single_site",
                    "pairing_style": "not_applicable",
                },
                "derivation": {"operator": "none", "output_role": "whole"},
                "vector_view": "unit_l2",
            },
        }
        with self.assertRaises(METHODS.MethodMetadataError):
            METHODS.resolve_method(spec)

    def test_confirmatory_design_requires_explicit_method(self) -> None:
        spec = {
            "name": "legacy-confirmatory",
            "direction": {"positive": "a", "negative": "b"},
            "prompts": ["x"],
            "design": {"intent": "confirmatory"},
        }
        method = METHODS.resolve_method(spec)
        with self.assertRaises(METHODS.MethodMetadataError):
            METHODS.resolve_design(spec, method)


if __name__ == "__main__":
    unittest.main()
