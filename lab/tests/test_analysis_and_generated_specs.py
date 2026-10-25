from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


METHODS = _load("generated_spec_methods", LAB / "nla_lab" / "methods.py")
TRANSCRIPTS = _load("transcript_analysis_tested", LAB / "analysis" / "transcripts.py")
CHOICES = _load("choice_analysis_tested", LAB / "analysis" / "choices.py")
WORDLE = _load("wordle_spec_generator_tested", LAB / "experiments" / "wordle2" / "gen_specs.py")
GEMMA = _load("gemma_spec_generator_tested", LAB / "experiments" / "gemma_core" / "gen_specs_gemma.py")


class AnalysisInputTests(unittest.TestCase):
    def test_non_run_json_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.json"
            path.write_text(json.dumps({"metric": 1.0}), encoding="utf-8")
            self.assertIsNone(TRANSCRIPTS.load_run(path))
            self.assertIsNone(CHOICES.load_run(path))

    def test_transcript_includes_resolved_method_provenance(self) -> None:
        data = {
            "spec": {
                "name": "inline-ar",
                "direction": {"positive": "house", "negative": "crane"},
            },
            "records": [],
        }
        rendered = TRANSCRIPTS.render(data)
        self.assertIn("Method: nla_ar_contrast / whole", rendered)
        self.assertIn("Method metadata: legacy_reviewed_backfill", rendered)


class GeneratedSpecMetadataTests(unittest.TestCase):
    def _assert_valid(self, spec: dict) -> tuple[dict, dict]:
        method = METHODS.resolve_method(spec)
        design = METHODS.resolve_design(spec, method)
        self.assertEqual(method["classification"]["origin"], "explicit_spec")
        self.assertEqual(design["classification_origin"], "explicit_spec")
        return method, design

    def test_every_wordle_arm_has_valid_explicit_metadata(self) -> None:
        for name, key, probes in WORDLE.ARMS:
            with self.subTest(name=name):
                spec = WORDLE.build_spec(name, key, probes)
                self._assert_valid(spec)

    def test_wordle_roots_controls_and_derived_ancestry(self) -> None:
        for key in ("len_four_b", "ff_dwell", "poor_lion_b"):
            method = WORDLE.method_for(key)
            self.assertEqual(method["root"]["method"], "nla_ar_contrast")
            self.assertEqual(method["root"]["pairing_style"], "matched_scaffold")

        component = WORDLE.method_for("comp25_ff_dwell")
        self.assertEqual(component["derivation"]["output_role"], "component")
        self.assertEqual(component["derivation"]["parent_vector_key"], "ff_dwell")
        remainder = WORDLE.method_for("resid25_ff_dwell")
        self.assertEqual(remainder["derivation"]["output_role"], "remainder")

        self.assertEqual(WORDLE.method_for(None)["root"]["method"], "none")
        self.assertEqual(
            WORDLE.design_for("RANDOM")["arm_role"], "negative_control"
        )

    def test_every_gemma_arm_has_valid_explicit_metadata(self) -> None:
        for name, key, prompts, probes in GEMMA.ARMS:
            with self.subTest(name=name):
                spec = GEMMA.build_spec(name, key, prompts, probes)
                self._assert_valid(spec)

    def test_gemma_parent_and_derived_ancestry(self) -> None:
        for key in ("g_dwell", "g_lion", "g_len_seven"):
            method = GEMMA.method_for(key)
            self.assertEqual(method["root"]["method"], "nla_ar_contrast")
            self.assertEqual(method["root"]["pairing_style"], "matched_scaffold")

        phrase = GEMMA.method_for("g_phrase_glyph")
        self.assertEqual(phrase["root"]["method"], "nla_av_edit_ar")
        self.assertEqual(phrase["root"]["edit_granularity"], "phrase")
        component = GEMMA.method_for("comp25_g_phrase_glyph")
        self.assertEqual(component["root"], phrase["root"])
        self.assertEqual(
            component["derivation"]["parent_vector_key"], "g_phrase_glyph"
        )
        self.assertEqual(component["derivation"]["output_role"], "component")


if __name__ == "__main__":
    unittest.main()
