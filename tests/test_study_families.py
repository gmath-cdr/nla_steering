"""Regression tests for the four added study families."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from study_families.common import ArtifactError, _safe_member_name
from study_families.selected_panel_judges import parse_rating_info, strict_harmonic
from study_families.verify import verify_all
from study_families.wordle import extract_answer, extract_leading_answer


class ArchiveSafetyTests(unittest.TestCase):
    def test_archive_member_paths_are_relative_and_bounded(self) -> None:
        self.assertEqual(_safe_member_name("run/results/file.json").as_posix(), "run/results/file.json")
        for unsafe in ("../secret.txt", "/absolute/path", "C:/drive/path", "..\\secret.txt"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ArtifactError):
                _safe_member_name(unsafe)


class JudgeRuleTests(unittest.TestCase):
    def test_rating_parser_preserves_missing_to_zero_rule(self) -> None:
        self.assertEqual(parse_rating_info("Rating: 2"), (2.0, "parsed"))
        self.assertEqual(parse_rating_info("No numeric answer"), (0.0, "missing_rating_to_zero"))

    def test_harmonic_is_zero_if_any_component_is_zero(self) -> None:
        self.assertEqual(strict_harmonic([2.0, 2.0, 0.0]), 0.0)
        self.assertAlmostEqual(strict_harmonic([1.0, 2.0, 2.0]), 1.5)


class WordleRuleTests(unittest.TestCase):
    def test_generic_and_leading_extractors_answer_different_claims(self) -> None:
        text = "**House** is a useful answer, if you like"
        self.assertEqual(extract_leading_answer(text), "house")
        self.assertEqual(extract_answer(text), "like")

    def test_archived_probe_prefers_explicit_caps(self) -> None:
        self.assertEqual(extract_answer("My answer is CRANE because it fits."), "crane")


class FullStudyVerificationTests(unittest.TestCase):
    def test_all_added_studies_rebuild_from_archived_inputs(self) -> None:
        summary = verify_all()
        self.assertEqual(len(summary["archives"]), 4)
        self.assertEqual(summary["freeform_wordle"]["scope"]["total_records"], 6995)
        self.assertEqual(
            summary["freeform_wordle"]["qwen_early_exact_house"]["leading_house_at_0.8"],
            25,
        )
        self.assertEqual(
            summary["selected_panel_judge_council"]["luna"]["parse_status_counts"],
            {"parsed": 10752},
        )


if __name__ == "__main__":
    unittest.main()
