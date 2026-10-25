from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np


LAB = Path(__file__).resolve().parents[1]
AXB_DIR = LAB / "experiments" / "axbench"
for path in (LAB, AXB_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nla_lab.jlens_atoms import (  # noqa: E402
    atom_from_readout,
    effective_scale_from_norm_response,
    tensor_f32_sha256,
    unit,
)
from nla_lab.probes import balanced_parentheticals  # noqa: E402
from nla_lab.study import (  # noqa: E402
    StudySpecError,
    canonical_text_file_sha256,
    normalize_prompts,
    seal_alpha_receipt,
    seal_spec,
    validate_alpha_receipt,
    validate_spec_integrity,
)
from nla_lab.target_metrics import (  # noqa: E402
    lexical_tokens,
    score_target,
)
from analysis import (  # noqa: E402
    HarnessError,
    evaluate_heldout_claim,
    evaluate_heldout_family,
    exact_one_sided_signflip_p,
    freeze_alpha_receipt,
    holm_adjust,
    make_alpha_receipt,
    paired_prompt_effects,
    select_alpha,
)
from harness import (  # noqa: E402
    build_spec,
    canonical_sha256,
    load_study,
    prompts_for,
    validate_prompt_bank,
    validate_source_registry,
    validate_study_status,
)
from gen_specs import build_canary_spec, validate_atom_report  # noqa: E402
from freeze_prompts import (  # noqa: E402
    build_adjudication_template,
    build_review_template,
    merge_reviews,
)


class TargetMetricTests(unittest.TestCase):
    def test_exact_word_boundaries_case_and_nfkc(self) -> None:
        metric = {"kind": "word", "target": "Meanwhile", "case_sensitive": False}
        scored = score_target(
            "MEANWHILE, meanwhile! meanwhileish. "
            "\uff2d\uff45\uff41\uff4e\uff57\uff48\uff49\uff4c\uff45.",
            metric,
        )
        self.assertEqual(scored["target_count"], 3.0)
        self.assertEqual(scored["target_present"], 1.0)

    def test_function_words_do_not_match_compounds(self) -> None:
        self.assertEqual(
            score_target("do don't undo doing do.", {"kind": "word", "target": "do"})[
                "target_count"
            ],
            2.0,
        )
        self.assertEqual(
            score_target("the other there THE", {"kind": "word", "target": "the"})[
                "target_count"
            ],
            2.0,
        )
        self.assertNotIn("super", lexical_tokens("supermarket superb"))

    def test_list_presence_requires_two_markers(self) -> None:
        metric = {
            "kind": "registered_probe", "probe": "list_markers",
            "minimum_presence_count": 2,
        }
        self.assertEqual(score_target("- one\nprose", metric)["target_present"], 0.0)
        self.assertEqual(score_target("- one\n- two", metric)["target_present"], 1.0)

    def test_balanced_parentheticals_exclude_author_year(self) -> None:
        text = (
            "A useful aside (plain context), citations (Smith, 2022) and "
            "(Smith et al., 2023), but not (broken."
        )
        self.assertEqual(balanced_parentheticals(text), 1)
        scored = score_target(text, {"kind": "citation_markers"})
        self.assertEqual(scored["generic_citation_marker_count"], 2.0)

    def test_supported_citation_labels_are_prompt_specific(self) -> None:
        scored = score_target(
            "Note A supports the claim, while Note B adds context.",
            {"kind": "citation_markers"},
            prompt={"allowed_attributions": ["Note A", "Note B"]},
        )
        self.assertEqual(scored["supported_attribution_count"], 2.0)
        self.assertEqual(scored["target_count"], 2.0)
        merged = score_target(
            "According to Note A, the claim holds. Study AB is unrelated.",
            {"kind": "citation_markers"},
            prompt={"allowed_attributions": ["Note A", "Study A"]},
        )
        self.assertEqual(merged["generic_citation_marker_count"], 1.0)
        self.assertEqual(merged["supported_attribution_count"], 1.0)
        self.assertEqual(merged["target_count"], 1.0)
        adjacent = score_target(
            "[1] [2] Note A Note B",
            {"kind": "citation_markers"},
            prompt={"allowed_attributions": ["Note A", "Note B"]},
        )
        self.assertEqual(adjacent["target_count"], 4.0)

    def test_unicode_coherence_catches_repetition(self) -> None:
        scored = score_target(
            "the the the the the useful answer",
            {"kind": "word", "target": "the"},
        )
        self.assertEqual(scored["max_token_run"], 5.0)
        self.assertGreater(scored["dominant_token_fraction"], 0.7)
        self.assertGreater(scored["target_token_fraction"], 0.7)


class PromptAndSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_study()

    def test_legacy_and_structured_prompts_normalize(self) -> None:
        rows = normalize_prompts([
            "legacy",
            {"id": "p2", "text": "structured", "split": "evaluation", "tag": 1},
        ])
        self.assertEqual(rows[0]["id"], "legacy-0000")
        self.assertEqual(rows[1]["tag"], 1)
        with self.assertRaises(StudySpecError):
            normalize_prompts([
                {"id": "x", "text": "one", "split": "calibration"},
                {"id": "x", "text": "two", "split": "evaluation"},
            ])

    def test_source_qualified_registry_and_prompt_counts(self) -> None:
        self.assertEqual(
            self.bundle["source"]["upstream"]["revision"],
            "41c8332543e5a631f9a8c0a9df38799893ace758",
        )
        self.assertEqual(len(self.bundle["concepts"]), 16)
        for key in self.bundle["study"]["pilot_concepts"]:
            counts = self.bundle["prompt_report"]["counts"][key]
            self.assertGreater(counts["calibration"], 0)
            self.assertGreater(counts["evaluation"], 0)

    def test_source_inventory_mismatch_fails_closed(self) -> None:
        registry = deepcopy(self.bundle["registry"])
        registry["source_inventory"] = "different_inventory"
        with self.assertRaises(HarnessError):
            validate_source_registry(self.bundle["source"], registry)

    def test_atom_ineligibility_receipt_is_model_scoped(self) -> None:
        space = self.bundle["study"]["models"]["qwen"]["space_id"]
        receipt = {
            "status": "ineligible_tokenization",
            "model_key": "qwen",
            "concept_key": "meanwhile",
            "space_id": space,
            "concept_registry_sha256": self.bundle["hashes"]["concepts"],
        }
        report = {
            "schema": "j-lens-atom-build/v2",
            "model_key": "qwen",
            "space_id": space,
            "concept_registry_sha256": self.bundle["hashes"]["concepts"],
            "receipts": {"meanwhile": receipt},
        }
        self.assertIs(
            validate_atom_report(self.bundle, "qwen", "meanwhile", report), receipt
        )
        report["model_key"] = "gemma3-12b"
        with self.assertRaises(HarnessError):
            validate_atom_report(self.bundle, "qwen", "meanwhile", report)

    def test_prompt_target_leakage_fails_closed(self) -> None:
        bank = deepcopy(self.bundle["prompt_bank"])
        bank["prompts"][0]["text"] += " Meanwhile."
        with self.assertRaises(HarnessError):
            validate_prompt_bank(
                bank, self.bundle["concepts"], self.bundle["study"]["pilot_concepts"]
            )

    def test_frozen_prompt_bank_requires_human_review_receipts(self) -> None:
        bank = deepcopy(self.bundle["prompt_bank"])
        bank["status"] = "frozen"
        with self.assertRaises(HarnessError):
            validate_prompt_bank(
                bank, self.bundle["concepts"], self.bundle["study"]["pilot_concepts"]
            )

    def test_two_completed_reviews_can_freeze_candidate_bank(self) -> None:
        review_a = build_review_template(self.bundle, "Reviewer A")
        review_b = build_review_template(self.bundle, "Reviewer B")
        adjudication = build_adjudication_template(self.bundle, "Adjudicator")
        for review in (review_a, review_b):
            for row in review["ratings"].values():
                row["target_absent"] = True
                row["behavior_optional"] = True
                row["affordance"] = {
                    key: 1 for key in row["affordance"]
                }
        for row in adjudication["ratings"].values():
            row["affordance"] = {
                key: (2 if key == "lists" else 1)
                for key in row["affordance"]
            }
        frozen = merge_reviews(
            self.bundle, review_a, review_b, adjudication
        )
        self.assertEqual(frozen["status"], "frozen")
        self.assertTrue(all("pre_output_review" in row for row in frozen["prompts"]))

    def test_frozen_study_cannot_retain_open_decisions(self) -> None:
        study = deepcopy(self.bundle["study"])
        study["status"] = "frozen"
        with self.assertRaises(HarnessError):
            validate_study_status(study)

    def test_draft_specs_are_screening_and_heldout_is_locked(self) -> None:
        spec = build_spec(
            self.bundle, model_key="qwen", concept_key="meanwhile",
            arm_id="nla_ar", phase="calibration", allow_draft=True,
        )
        self.assertEqual(spec["design"]["intent"], "screening")
        self.assertFalse(spec["design"]["preregistered"])
        self.assertEqual(spec["expected_runtime"]["layer"], 20)
        self.assertTrue(all(isinstance(prompt, dict) for prompt in spec["prompts"]))
        self.assertEqual(set(spec["monitor_metrics"]), set(self.bundle["study"]["pilot_concepts"]))
        self.assertEqual(
            spec["expected_records"],
            len(spec["prompts"]) * len(spec["alphas"]) * spec["samples_per_prompt"],
        )
        validate_spec_integrity(spec, required=True)
        changed = deepcopy(spec)
        changed["prompts"][0]["text"] += " edited"
        with self.assertRaises(StudySpecError):
            validate_spec_integrity(changed, required=True)
        with self.assertRaises(HarnessError):
            build_spec(
                self.bundle, model_key="qwen", concept_key="meanwhile",
                arm_id="nla_ar", phase="evaluation", allow_draft=True,
            )

    def test_list_suppression_uses_negative_calibration_alphas(self) -> None:
        spec = build_spec(
            self.bundle, model_key="qwen", concept_key="lists",
            arm_id="nla_ar", phase="calibration", allow_draft=True,
        )
        self.assertEqual(spec["study"]["effect_direction"], "decrease")
        self.assertTrue(all(alpha <= 0 for alpha in spec["alphas"]))

    def test_disabled_gemma_model_fails_before_materialization(self) -> None:
        with self.assertRaises(HarnessError):
            build_spec(
                self.bundle, model_key="gemma3-12b", concept_key="meanwhile",
                arm_id="nla_ar", phase="calibration", allow_draft=True,
            )

    def test_baseline_has_only_alpha_zero_and_matching_cardinality(self) -> None:
        spec = build_spec(
            self.bundle, model_key="qwen", concept_key="meanwhile",
            arm_id="baseline", phase="calibration", allow_draft=True,
        )
        self.assertEqual(spec["alphas"], [0.0])
        self.assertEqual(
            spec["expected_records"],
            len(spec["prompts"]) * spec["samples_per_prompt"],
        )

    def test_canary_is_tiny_sealed_and_external_output_guarded(self) -> None:
        spec = build_canary_spec(self.bundle, "qwen", "meanwhile")
        self.assertEqual(len(spec["prompts"]), 1)
        self.assertEqual(len(spec["alphas"]), 2)
        self.assertEqual(spec["samples_per_prompt"], 1)
        self.assertEqual(spec["expected_records"], 2)
        self.assertTrue(spec["study"]["canary"])
        self.assertTrue(spec["execution_guard"]["require_clean_git"])
        self.assertTrue(spec["execution_guard"]["require_external_output_dir"])
        validate_spec_integrity(spec, required=True)

    def test_heldout_baseline_still_requires_alpha_receipt(self) -> None:
        bundle = deepcopy(self.bundle)
        bundle["study"]["status"] = "frozen"
        bundle["prompt_report"]["status"] = "frozen"
        with self.assertRaises(HarnessError):
            build_spec(
                bundle, model_key="qwen", concept_key="meanwhile",
                arm_id="baseline", phase="evaluation",
            )

    def test_pure_atom_has_explicit_root_and_integrity_lock(self) -> None:
        receipt = {
            "status": "built", "model_key": "qwen", "concept_key": "meanwhile",
            "space_id": "qwen2.5-7b-instruct.resid-post.L20.d3584",
            "layer": 20, "hidden_width": 3584,
            "lens_sha256": "84564f848c839d97eb0412ea85ae6a9b0f09f83c1ff75439fdc1966805367bd8",
            "concept_registry_sha256": self.bundle["hashes"]["concepts"],
            "atom_formula": "j-lens-atom-effective-final-norm/v2",
            "effective_norm_class": "FakeQwenRMSNorm",
            "effective_norm_response_f32_sha256": "e" * 64,
            "checkpoint_marker_sha256": {"config.json": "f" * 64},
            "unembedding_row_f32_sha256": "9" * 64,
            "token": {
                "chosen_variant": "space", "token_id": 1, "decoded": " Meanwhile",
                "resolutions": {
                    "space": {"surface": " Meanwhile", "ids": [1], "single": True},
                    "bare": {"surface": "Meanwhile", "ids": [2], "single": True},
                },
            },
            "archive_path": "experiments/axbench/data/qwen_L20_j_atoms.npz",
            "archive_member": "meanwhile__space",
            "source_archive_sha256": "a" * 64,
            "source_member_f32_sha256": "b" * 64,
            "unit_direction_sha256": "c" * 64,
            "split_half_available": True,
            "split_half_atom_cosine": 0.95,
            "split_lens_sha256": {"A": "1" * 64, "B": "2" * 64},
        }
        spec = build_spec(
            self.bundle, model_key="qwen", concept_key="meanwhile",
            arm_id="direct_j_atom", phase="calibration", atom_receipt=receipt,
            allow_draft=True,
        )
        self.assertEqual(spec["method"]["root"]["method"], "j_lens_atom")
        self.assertEqual(
            spec["expected_direction_integrity"]["unit_direction_sha256"], "c" * 64
        )
        self.assertEqual(
            spec["expected_atom_runtime"]["unembedding_row_f32_sha256"], "9" * 64
        )


class JLensAtomTests(unittest.TestCase):
    def test_atom_formula_and_norm_parameterizations(self) -> None:
        J = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        row = np.asarray([5.0, 7.0], dtype=np.float32)
        qwen_weight = np.asarray([1.2, 0.8], dtype=np.float32)
        gemma_parameter = np.asarray([0.2, -0.2], dtype=np.float32)
        qwen_scale = effective_scale_from_norm_response(qwen_weight, 2)
        gemma_scale = effective_scale_from_norm_response(1.0 + gemma_parameter, 2)
        np.testing.assert_allclose(qwen_scale, gemma_scale)
        expected = J.T @ (qwen_scale * row)
        np.testing.assert_allclose(atom_from_readout(J, qwen_scale, row), expected)

    def test_unit_hash_is_representation_stable(self) -> None:
        value = np.asarray([3.0, 4.0], dtype=np.float64)
        self.assertEqual(tensor_f32_sha256(unit(value)), tensor_f32_sha256([0.6, 0.8]))

    def test_code_hash_normalizes_windows_newlines(self) -> None:
        with TemporaryDirectory() as directory:
            lf = Path(directory) / "lf.py"
            crlf = Path(directory) / "crlf.py"
            lf.write_bytes(b"a\nb\n")
            crlf.write_bytes(b"a\r\nb\r\n")
            self.assertEqual(
                canonical_text_file_sha256(lf), canonical_text_file_sha256(crlf)
            )


class AlphaSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_study()

    def _result(self, phase: str = "calibration") -> dict:
        spec = build_spec(
            self.bundle, model_key="qwen", concept_key="meanwhile",
            arm_id="nla_ar", phase="calibration", allow_draft=True,
        )
        spec["study"]["phase"] = phase
        spec["alphas"] = [0.0, 0.1, 0.2]
        spec["samples_per_prompt"] = 2
        spec["expected_records"] = len(spec["prompts"]) * 3 * 2
        spec["design"]["preregistered"] = True
        spec = seal_spec(spec)
        records = []
        for prompt_index, prompt_id in enumerate(
            row["id"] for row in spec["prompts"]
        ):
            for sample in (2 * prompt_index, 2 * prompt_index + 1):
                for alpha, rate, run in ((0.0, 0.0, 1), (0.1, 1.0, 1), (0.2, 2.0, 9)):
                    records.append({
                        "prompt_id": prompt_id, "sample": sample, "alpha": alpha,
                        "prompt_split": "calibration",
                        "target_metrics": {
                            "target_rate_per_100_words": rate,
                            "unicode_word_count": 100.0,
                            "unique_unicode_word_ratio": 0.7,
                            "max_token_run": float(run),
                            "dominant_token_fraction": 0.05,
                            "max_character_run": 2.0,
                            "replacement_character_count": 0.0,
                            "target_token_fraction": rate / 100,
                        },
                    })
        return {
            "schema": "steering-run/v2",
            "spec": spec,
            "method": {"root": {"method": "nla_ar_contrast"}},
            "runtime": {
                "model_key": "qwen", "git": {"dirty": False, "commit": "f" * 40},
                "package_versions": {"torch": "test"},
                "checkpoint_marker_sha256": {
                    "base": {"config.json": "a" * 64, "tokenizer.json": "b" * 64}
                },
            },
            "design": {"preregistered": True},
            "direction_integrity": {
                "schema": "steering-tensor-f32-le/v1",
                "unit_direction_sha256": "c" * 64,
            },
            "records": records,
            "_sha256": "d" * 64,
        }

    def test_pairing_and_safety_gated_selection(self) -> None:
        result = self._result()
        self.assertEqual(
            set(paired_prompt_effects(result, 0.1).values()), {1.0}
        )
        selected = select_alpha(result, self.bundle)
        self.assertEqual(selected["chosen_alpha"], 0.1)

    def test_selection_rejects_an_incomplete_matrix(self) -> None:
        result = self._result()
        result["records"].pop()
        with self.assertRaises(HarnessError):
            select_alpha(result, self.bundle)

    def test_receipt_rejects_method_metadata_disagreement(self) -> None:
        result = self._result()
        result["method"]["root"]["method"] = "random_isotropic"
        with self.assertRaises(HarnessError):
            make_alpha_receipt([result], self.bundle)

    def test_evaluation_rows_cannot_select_alpha(self) -> None:
        with self.assertRaises(HarnessError):
            select_alpha(self._result("evaluation"), self.bundle)

    def test_receipt_requires_raw_audit_before_freezing(self) -> None:
        # This fixture exercises receipt mechanics with an NLA-only concept;
        # token-eligible concepts correctly require a direct-J calibration too.
        bundle = deepcopy(self.bundle)
        bundle["concepts"]["meanwhile"].pop("direct_j_atom")
        receipt = make_alpha_receipt([self._result()], bundle)
        self.assertEqual(receipt["status"], "pending_raw_audit")
        validate_alpha_receipt(receipt)
        with self.assertRaises(HarnessError):
            freeze_alpha_receipt(receipt, reviewer="", audit_note="looked")
        frozen = freeze_alpha_receipt(
            receipt, reviewer="reviewer", audit_note="Reviewed every calibration output."
        )
        self.assertEqual(frozen["status"], "frozen")
        validate_alpha_receipt(frozen, require_frozen=True)
        tampered = deepcopy(frozen)
        tampered["chosen_alpha_by_arm"]["nla_ar"] = 0.7
        with self.assertRaises(StudySpecError):
            validate_alpha_receipt(tampered, require_frozen=True)

    def test_direct_atom_must_be_calibrated_or_formally_excluded(self) -> None:
        result = self._result()
        with self.assertRaises(HarnessError):
            make_alpha_receipt([result], self.bundle)
        declaration = self.bundle["concepts"]["meanwhile"]["direct_j_atom"]
        exclusion = {
            "status": "ineligible_tokenization",
            "model_key": "qwen",
            "concept_key": "meanwhile",
            "space_id": self.bundle["study"]["models"]["qwen"]["space_id"],
            "concept_registry_sha256": self.bundle["hashes"]["concepts"],
            "checkpoint_marker_sha256": result["runtime"][
                "checkpoint_marker_sha256"
            ]["base"],
            "token_text": declaration["token_text"],
            "requested_variants": [
                declaration["preferred_variant"],
                declaration["fallback_variant"],
            ],
            "reason": "fixture is not one token",
        }
        receipt = make_alpha_receipt(
            [result], self.bundle, direct_atom_exclusion=exclusion
        )
        self.assertEqual(
            receipt["treatment_exclusions"]["direct_j_atom"]["reason"],
            "ineligible_tokenization",
        )
        validate_alpha_receipt(receipt)

    def test_receipt_rejects_a_stale_preregistration_lock(self) -> None:
        result = self._result()
        result["spec"]["study"]["locks"]["prompts"] = "stale"
        result["spec"] = seal_spec(result["spec"])
        with self.assertRaises(HarnessError):
            make_alpha_receipt([result], self.bundle)


class HeldoutEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_study()

    def _alpha_receipt(self, *, direct_exclusion: dict | None = None) -> dict:
        treatment_exclusions = {}
        if direct_exclusion is not None:
            treatment_exclusions["direct_j_atom"] = {
                "reason": "ineligible_tokenization",
                "receipt": direct_exclusion,
                "receipt_sha256": canonical_sha256(direct_exclusion),
            }
        return seal_alpha_receipt({
            "schema": "axbench-alpha-receipt/v1",
            "status": "frozen",
            "analysis": "axbench-paired-prompt-blocked/v1",
            "selection_rule": "max-safe-signed-effect-smallest-near-best/v1",
            "model_key": "qwen",
            "concept_key": "meanwhile",
            "chosen_alpha_by_arm": {"nla_ar": 0.2},
            "random_control_alphas": [0.2],
            "selection_details": {"nla_ar": {"chosen_alpha": 0.2}},
            "calibration_result_sha256": {"nla_ar": "a" * 64},
            "lock_bundle_sha256": canonical_sha256(self.bundle["hashes"]),
            "base_checkpoint_marker_sha256": {
                "config.json": "c" * 64,
                "tokenizer.json": "d" * 64,
            },
            "treatment_direction_integrity": {
                "nla_ar": {"unit_direction_sha256": "e" * 64}
            },
            "treatment_exclusions": treatment_exclusions,
            "calibration_git_commit": "f" * 40,
            "calibration_package_versions": {"torch": "test"},
            "created_utc": "2026-08-22T00:00:00Z",
            "audit": {
                "reviewer": "Fixture Reviewer",
                "note": "Fixture outputs reviewed.",
                "frozen_utc": "2026-08-22T00:01:00Z",
            },
        })

    def _result(
        self, arm_id: str, effect: float, *, direct_exclusion: dict | None = None
    ) -> dict:
        spec = build_spec(
            self.bundle,
            model_key="qwen",
            concept_key="meanwhile",
            arm_id=arm_id,
            phase="calibration",
            allow_draft=True,
        )
        spec["study"]["phase"] = "evaluation"
        spec["prompts"] = prompts_for(
            self.bundle["prompt_bank"], "meanwhile", "evaluation"
        )
        spec["alphas"] = [0.0] if arm_id == "baseline" else [0.0, 0.2]
        spec["samples_per_prompt"] = 2
        spec["expected_records"] = (
            len(spec["prompts"]) * len(spec["alphas"]) * 2
        )
        spec["design"]["preregistered"] = True
        alpha_receipt = self._alpha_receipt(direct_exclusion=direct_exclusion)
        spec["study"]["alpha_receipt"] = alpha_receipt
        spec["study"]["alpha_receipt_sha256"] = canonical_sha256(alpha_receipt)
        spec = seal_spec(spec)
        records = []
        for prompt_index, prompt in enumerate(spec["prompts"]):
            for sample in (2 * prompt_index, 2 * prompt_index + 1):
                for alpha in spec["alphas"]:
                    rate = effect if alpha else 0.0
                    records.append({
                        "prompt_id": prompt["id"],
                        "prompt_split": "evaluation",
                        "prompt": prompt["text"],
                        "sample": sample,
                        "alpha": alpha,
                        "text": (
                            f"baseline::{prompt['id']}::{sample}"
                            if alpha == 0 else f"steered::{arm_id}::{prompt['id']}::{sample}"
                        ),
                        "target_metrics": {
                            "target_count": rate,
                            "target_present": float(rate > 0),
                            "target_rate_per_100_words": rate,
                            "unicode_word_count": 100.0,
                            "unique_unicode_word_ratio": 0.7,
                            "max_token_run": 1.0,
                            "dominant_token_fraction": 0.05,
                            "max_character_run": 2.0,
                            "replacement_character_count": 0.0,
                            "target_token_fraction": rate / 100.0,
                        },
                    })
        return {
            "schema": "steering-run/v2",
            "spec": spec,
            "runtime": {"model_key": "qwen"},
            "records": records,
        }

    def test_exact_signflip_and_holm_helpers(self) -> None:
        effects = {f"p{i}": 1.0 for i in range(8)}
        self.assertEqual(exact_one_sided_signflip_p(effects), 1 / 256)
        adjusted = holm_adjust({"a": 0.01, "b": 0.03, "c": 0.2})
        self.assertAlmostEqual(adjusted["a"], 0.03)
        self.assertAlmostEqual(adjusted["b"], 0.06)
        self.assertAlmostEqual(adjusted["c"], 0.2)

    def test_claim_beats_paired_zero_and_mean_random(self) -> None:
        treatment = self._result("nla_ar", 2.0)
        baseline = self._result("baseline", 0.0)
        randoms = [
            self._result("random_101", 0.2),
            self._result("random_202", 0.3),
            self._result("random_303", 0.1),
        ]
        outcome = evaluate_heldout_claim(
            treatment, randoms, baseline, self.bundle
        )
        self.assertAlmostEqual(outcome["signed_effect_vs_baseline"], 2.0)
        self.assertAlmostEqual(outcome["signed_effect_vs_random_mean"], 1.8)
        self.assertEqual(outcome["intersection_union_p"], 1 / 256)
        self.assertTrue(outcome["passes_safety"])

    def test_alpha_zero_mismatch_fails_closed(self) -> None:
        treatment = self._result("nla_ar", 2.0)
        baseline = self._result("baseline", 0.0)
        randoms = [
            self._result("random_101", 0.2),
            self._result("random_202", 0.3),
            self._result("random_303", 0.1),
        ]
        randoms[0]["records"][0]["text"] = "different alpha-zero output"
        with self.assertRaises(HarnessError):
            evaluate_heldout_claim(treatment, randoms, baseline, self.bundle)

    def test_family_accepts_only_a_sealed_tokenization_exclusion(self) -> None:
        declaration = self.bundle["concepts"]["meanwhile"]["direct_j_atom"]
        exclusion = {
            "status": "ineligible_tokenization",
            "model_key": "qwen",
            "concept_key": "meanwhile",
            "space_id": self.bundle["study"]["models"]["qwen"]["space_id"],
            "concept_registry_sha256": self.bundle["hashes"]["concepts"],
            "checkpoint_marker_sha256": {
                "config.json": "c" * 64,
                "tokenizer.json": "d" * 64,
            },
            "token_text": declaration["token_text"],
            "requested_variants": [
                declaration["preferred_variant"],
                declaration["fallback_variant"],
            ],
            "reason": "fixture is not one token",
        }
        rows = [
            self._result("baseline", 0.0, direct_exclusion=exclusion),
            self._result("nla_ar", 2.0, direct_exclusion=exclusion),
            self._result("random_101", 0.2, direct_exclusion=exclusion),
            self._result("random_202", 0.3, direct_exclusion=exclusion),
            self._result("random_303", 0.1, direct_exclusion=exclusion),
        ]
        bundle = deepcopy(self.bundle)
        bundle["study"]["primary_claims"] = [
            {"id": "meanwhile__nla_ar", "concept": "meanwhile", "arm": "nla_ar"},
            {
                "id": "meanwhile__direct_j_atom",
                "concept": "meanwhile",
                "arm": "direct_j_atom",
                "eligibility": "single_token_receipt",
            },
        ]
        summary = evaluate_heldout_family(rows, bundle)
        self.assertEqual(summary["realized_family_size"], 1)
        self.assertEqual(
            summary["excluded_claims"][0]["claim_id"],
            "meanwhile__direct_j_atom",
        )


if __name__ == "__main__":
    unittest.main()
