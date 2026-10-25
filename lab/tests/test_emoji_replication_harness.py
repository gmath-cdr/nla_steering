from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


LAB = Path(__file__).resolve().parents[1]
CAMPAIGN = LAB / "experiments" / "emoji_replication"
sys.path.insert(0, str(LAB))
from experiments.emoji_replication.harness import (  # noqa: E402
    HarnessError, _validate_prompts, build_canary_spec, build_spec, load_study,
)
from experiments.emoji_replication.freeze_prompts import (  # noqa: E402
    build_adjudication_template, build_review_template, merge_reviews,
)
from nla_lab.study import validate_spec_integrity  # noqa: E402


class EmojiReplicationHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_study()

    def test_prompt_matrix_and_sources_validate(self) -> None:
        self.assertEqual(self.bundle["prompt_report"]["count"], 34)
        self.assertEqual(set(self.bundle["sources"]), {
            "av_emoji", "av_glyph", "av_phrase", "ar_freeform", "av_excited",
        })
        self.assertFalse(self.bundle["frozen"])

    def test_duplicate_emoji_alias_is_not_an_arm(self) -> None:
        self.assertNotIn("s2_emoji", self.bundle["sources"])
        excluded = self.bundle["source"]["excluded_duplicates"]
        self.assertEqual(len(excluded), 1)

    def test_research_spec_fails_while_prompt_review_is_draft(self) -> None:
        with self.assertRaisesRegex(HarnessError, "frozen prompt review"):
            build_spec(self.bundle, arm_id="av_emoji", phase="calibration")

    def test_canary_is_sealed_and_excluded(self) -> None:
        spec = build_canary_spec(self.bundle)
        validate_spec_integrity(spec, required=True)
        self.assertTrue(spec["study"]["excluded_from_research_corpus"])
        self.assertEqual(spec["expected_records"], 2)
        self.assertEqual(spec["alphas"], [0.0, 0.1])
        self.assertEqual(spec["prompts"][0]["text"], "Tell me about your day.")

    def test_in_memory_frozen_calibration_cardinality(self) -> None:
        bundle = deepcopy(self.bundle)
        bundle["frozen"] = True
        bundle["study"]["status"] = "frozen"
        bundle["prompt_report"]["status"] = "frozen"
        spec = build_spec(bundle, arm_id="av_glyph", phase="calibration")
        self.assertEqual(spec["expected_records"], 126)
        self.assertEqual(spec["method"]["root"]["method"], "nla_av_edit_ar")
        self.assertEqual(spec["method"]["root"]["edit_granularity"], "single_site")

    def test_ar_and_random_taxonomy_are_distinct(self) -> None:
        bundle = deepcopy(self.bundle)
        bundle["frozen"] = True
        ar = build_spec(bundle, arm_id="ar_freeform", phase="calibration")
        random = build_spec(
            bundle, arm_id="random_101", phase="calibration", alphas=[0, 0.3]
        )
        self.assertEqual(ar["method"]["root"]["method"], "nla_ar_contrast")
        self.assertEqual(random["method"]["root"]["method"], "random_isotropic")
        self.assertEqual(random["direction_seed"], 101)

    def test_source_manifest_pins_unit_hash(self) -> None:
        source = CAMPAIGN / "source_manifest.yaml"
        original = source.read_text(encoding="utf-8")
        self.assertIn(
            "432eda9b9985d487e66884e2a5c74a9ee1ffd0179ab5250c681a60f49f236f0f",
            original,
        )

    def test_status_flip_cannot_bypass_human_receipts(self) -> None:
        bank = deepcopy(self.bundle["prompt_bank"])
        bank["status"] = "frozen"
        with self.assertRaisesRegex(HarnessError, "review receipt"):
            _validate_prompts(self.bundle["study"], bank)

    def test_heldout_materialization_is_fail_closed_until_evaluator_exists(self) -> None:
        bundle = deepcopy(self.bundle)
        bundle["frozen"] = True
        with self.assertRaisesRegex(HarnessError, "family evaluator"):
            build_spec(
                bundle, arm_id="av_emoji", phase="evaluation", alphas=[0, 0.3]
            )

    def test_two_distinct_completed_reviews_can_freeze_bank(self) -> None:
        a = build_review_template(self.bundle, "Reviewer A")
        b = build_review_template(self.bundle, "Reviewer B")
        final = build_adjudication_template(self.bundle, "Adjudicator")
        prompt_by_id = {
            prompt["id"]: prompt for prompt in self.bundle["prompt_report"]["prompts"]
        }
        for review in (a, b):
            for row in review["ratings"].values():
                row.update(
                    target_absent=True, behavior_not_required=True,
                    answerable=True, category_fit=True,
                )
            for prompt_id, row in review["ratings"].items():
                row["emoji_affordance"] = (
                    0 if prompt_by_id[prompt_id]["split"] == "transfer" else 1
                )
        for prompt_id, row in final["ratings"].items():
            row["emoji_affordance"] = (
                0 if prompt_by_id[prompt_id]["split"] == "transfer" else 1
            )
        frozen = merge_reviews(self.bundle, a, b, final)
        self.assertEqual(frozen["status"], "frozen")
        self.assertIn("pre_output_review", frozen["prompts"][0])

    def test_duplicate_reviewers_cannot_freeze_bank(self) -> None:
        a = build_review_template(self.bundle, "Same Person")
        b = build_review_template(self.bundle, "Same Person")
        final = build_adjudication_template(self.bundle, "Adjudicator")
        for review in (a, b):
            for row in review["ratings"].values():
                row.update(
                    target_absent=True, behavior_not_required=True,
                    answerable=True, category_fit=True, emoji_affordance=1,
                )
        for row in final["ratings"].values():
            row["emoji_affordance"] = 1
        with self.assertRaisesRegex(HarnessError, "distinct"):
            merge_reviews(self.bundle, a, b, final)


if __name__ == "__main__":
    unittest.main()
