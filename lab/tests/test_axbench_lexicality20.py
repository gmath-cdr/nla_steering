from __future__ import annotations

import sys
from pathlib import Path
import unittest


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.axbench_lexicality20.gen_specs import (  # noqa: E402
    calibration_specs, evaluation_specs,
)
from experiments.axbench_lexicality20.judge import harmonic, parse_rating  # noqa: E402
from experiments.axbench_lexicality20.protocol import (  # noqa: E402
    append_concept_edit, assignments, generation_seeds, load_bundle,
    prompt_instruction, stable_seed, visible_prompt,
)
from experiments.axbench_lexicality20.select_alpha import choose  # noqa: E402
from nla_lab.methods import resolve_design, resolve_method  # noqa: E402


class AxbenchLexicality20Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundle()
        cls.verification = {
            "model_key": "qwen", "review_payload_sha256": cls.bundle["review_payload_sha256"],
            "checkpoints": {"base": {"marker_sha256": {"config.json": "a" * 64}}},
        }
        members = {}
        for concept in cls.bundle["campaign"]["concepts"]:
            cid = int(concept["concept_id"])
            for arm in ("diffmean", "caa"):
                members[f"c{cid}.{arm}"] = {"unit_direction_f32_sha256": "b" * 64}
            for split in ("calibration", "evaluation"):
                for prompt in assignments(cls.bundle, concept_id=cid, split=split):
                    members[f"c{cid}.{prompt['prompt_id']}.nla"] = {
                        "unit_direction_f32_sha256": "b" * 64
                    }
        cls.report = {
            "model_key": "qwen", "review_payload_sha256": cls.bundle["review_payload_sha256"],
            "archive": "/workspace/axb44/directions.npz", "members": members,
        }

    def test_approved_panel_and_assignments(self) -> None:
        self.assertEqual(len(self.bundle["campaign"]["concepts"]), 10)
        self.assertEqual(len(assignments(self.bundle, concept_id=362, split="calibration")), 4)
        self.assertEqual(len(assignments(self.bundle, concept_id=362, split="evaluation")), 4)
        self.assertIn("surprise", prompt_instruction(self.bundle, 362).lower())

    def test_visible_prompt_is_exact_frozen_format(self) -> None:
        carrier = "How does an LCD panel work?"
        value = visible_prompt(self.bundle, 362, carrier)
        self.assertEqual(value, prompt_instruction(self.bundle, 362) + "\n\nQuestion: " + carrier)

    def test_seed_identity_is_stable_and_concept_specific(self) -> None:
        first = generation_seeds(
            self.bundle, model_key="qwen", concept_id=362,
            prompt_id="alpaca-0011", sample_count=2,
        )
        self.assertEqual(first, generation_seeds(
            self.bundle, model_key="qwen", concept_id=362,
            prompt_id="alpaca-0011", sample_count=2,
        ))
        self.assertEqual(len(set(first)), 2)
        self.assertNotEqual(first[0], stable_seed("generation", "other"))

    def test_edit_location(self) -> None:
        original = "<explanation>Base text.</explanation>"
        edited = append_concept_edit(original, "The representation specifically concerns: surprise.")
        self.assertEqual(
            edited,
            "<explanation>Base text.\n\nThe representation specifically concerns: surprise.\n</explanation>",
        )

    def test_calibration_specs_match_approved_920(self) -> None:
        specs = calibration_specs(
            self.bundle, self.verification, self.report, model_key="qwen"
        )
        self.assertEqual(sum(spec["expected_records"] for spec in specs), 920)
        self.assertTrue(all(spec.get("spec_integrity") for spec in specs))
        nla = next(spec for spec in specs if spec["study"]["arm_id"] == "nla")
        self.assertEqual(nla["mode"], "last_and_decode")
        self.assertTrue(nla["record_target_model_perplexity"])
        fixed = next(spec for spec in specs if spec["study"]["arm_id"] == "diffmean")
        self.assertEqual(fixed["mode"], "all")
        self.assertNotIn(0.0, fixed["alphas"])
        self.assertEqual(len(fixed["linked_baseline_spec_sha256"]), 64)

    def test_evaluation_is_receipt_locked_and_within_cap(self) -> None:
        receipt = {
            "chosen_alpha": {
                str(concept["concept_id"]): {"diffmean": 0.1, "caa": 0.2, "nla": 0.3}
                for concept in self.bundle["campaign"]["concepts"]
            }
        }
        specs = evaluation_specs(
            self.bundle, self.verification, self.report, receipt, model_key="qwen"
        )
        self.assertLessEqual(sum(spec["expected_records"] for spec in specs), 1120)
        self.assertEqual(sum(
            spec["expected_records"] for spec in specs
            if spec["study"]["arm_id"] == "unsteered"
        ), 80)
        randoms = [spec for spec in specs if spec["method"]["root"]["method"] == "random_isotropic"]
        self.assertTrue(randoms)
        self.assertTrue(all(len(spec["alphas"]) == 1 for spec in randoms))

    def test_new_method_roots_and_scope_validate(self) -> None:
        spec = calibration_specs(
            self.bundle, self.verification, self.report, model_key="qwen"
        )[2]
        resolved = resolve_method(spec)
        self.assertEqual(resolved["root"]["method"], "diffmean_activation_contrast")
        self.assertEqual(resolved["intervention"]["token_scope"], "prompt_and_decode_all")
        self.assertEqual(resolve_design(spec, resolved)["arm_role"], "treatment")

    def test_axbench_parser_and_harmonic(self) -> None:
        self.assertEqual(parse_rating("Explanation.\nRating: [[2]]"), 2.0)
        self.assertEqual(parse_rating("Rating: [[9]]"), 0.0)
        self.assertEqual(parse_rating("No parse"), 0.0)
        self.assertAlmostEqual(harmonic([2, 2, 1]), 1.5)
        self.assertEqual(harmonic([2, 0, 2]), 0.0)

    def test_alpha_choice_uses_mean_and_smaller_tie(self) -> None:
        rows = [
            {"alpha": 0.1, "axbench_harmonic": value} for value in (1, 1, 1, 1)
        ] + [
            {"alpha": 0.2, "axbench_harmonic": value} for value in (1, 1, 1, 1)
        ]
        selected, details = choose(rows, [0.1, 0.2])
        self.assertEqual(selected, 0.1)
        self.assertEqual(len(details), 2)

    def test_caa_fallback_is_blocked_by_archived_model_time_parity(self) -> None:
        source = (
            LAB / "experiments" / "axbench_lexicality20" / "build_directions.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CAA_PARITY_MIN_COSINE = 0.99", source)
        self.assertIn("validate_caa_parity(", source)
        self.assertIn("historical reproduction check", source)
        self.assertIn("AXB44_CAA_REFERENCE_DIR", source)
        self.assertIn("archived_caa_reference(model_key)", source)
        self.assertIn("CAA extractor failed archived parity", source)


if __name__ == "__main__":
    unittest.main()
