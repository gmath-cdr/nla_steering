from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_method_comparison.method_lock import (  # noqa: E402
    GLYPH,
    LOCK_PATH,
    PROMPT_BANK_PATH,
    MethodLockError,
    load_method_lock,
    load_prompt_bank,
    validate_method_lock,
    validate_prompt_bank,
)
from experiments.emoji_method_comparison.freeze_data import (  # noqa: E402
    CAA_PATH,
    CALIBRATION_PATH,
    DataFreezeError,
    load_yaml,
    validate_caa_manifest,
    validate_calibration_bank,
)


class EmojiMethodLockTests(unittest.TestCase):
    def test_checked_in_lock_is_valid(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        self.assertEqual(lock["target"]["glyph_surface"], GLYPH)
        self.assertEqual(len(GLYPH), 1)

    def test_nla_is_not_averaged(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        self.assertEqual(lock["construction"]["nla_phrase"]["averaging"], "none")
        self.assertEqual(lock["construction"]["nla_glyph"]["averaging"], "none")

    def test_phrase_or_glyph_drift_fails(self) -> None:
        for arm, replacement in (("nla_phrase", "Use emoji."), ("nla_glyph", GLYPH * 2)):
            with self.subTest(arm=arm):
                lock = deepcopy(load_method_lock(LOCK_PATH))
                lock["construction"][arm]["appended_text"] = replacement
                with self.assertRaises(MethodLockError):
                    validate_method_lock(lock)

    def test_caa_balance_and_mean_are_required(self) -> None:
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["construction"]["caa"]["option_balance"] = "unbalanced"
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)

    def test_jlens_cannot_silently_become_a_glyph_combination(self) -> None:
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["construction"]["jlens_word_atom"]["glyph_diagnostic"]["behavioral_arm"] = True
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)

    def test_jlens_is_preserved_but_not_a_v5_behavioral_arm(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        primary = [row["id"] for row in lock["arms"]["primary_treatments"]]
        exploratory = lock["arms"]["exploratory_preserved"]
        self.assertNotIn("jlens_word_atom", primary)
        self.assertEqual(exploratory[0]["id"], "jlens_word_atom")
        self.assertIs(exploratory[0]["behavioral_generation"], False)
        self.assertNotIn(
            "jlens_word_atom",
            lock["evaluation"]["primary_claim_multiplicity_family"],
        )

    def test_common_intervention_cannot_drift_to_all_positions(self) -> None:
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["common_intervention"]["mode"] = "all"
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)

    def test_v3_ceiling_and_failed_arm_policy_are_locked(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        self.assertEqual(lock["common_intervention"]["max_new_tokens"], 192)
        self.assertEqual(
            lock["calibration_selection"]["failed_arm_policy"],
            "record_no_safe_alpha_and_exclude_from_heldout",
        )
        drifted = deepcopy(lock)
        drifted["common_intervention"]["max_new_tokens"] = 96
        with self.assertRaises(MethodLockError):
            validate_method_lock(drifted)

    def test_two_model_spaces_are_frozen_and_never_transport_vectors(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        matrix = lock["model_matrix"]
        self.assertEqual(matrix["qwen"]["hidden_width"], 3584)
        self.assertEqual(matrix["gemma3-12b"]["hidden_width"], 3840)
        self.assertEqual(matrix["qwen"]["layer"], 20)
        self.assertEqual(matrix["gemma3-12b"]["layer"], 32)
        self.assertEqual(matrix["qwen"]["calibration_alphas"][-1], 0.6)
        self.assertEqual(matrix["gemma3-12b"]["calibration_alphas"], [0, 0.05, 0.1, 0.15, 0.2])
        self.assertEqual(
            matrix["invariants"]["vector_transport_between_models"],
            "forbidden",
        )

    def test_model_space_or_alpha_pooling_drift_fails(self) -> None:
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["model_matrix"]["gemma3-12b"]["hidden_width"] = 3584
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["model_matrix"]["qwen"]["checkpoint_sources"]["ar"]["revision"] = "main"
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)
        lock = deepcopy(load_method_lock(LOCK_PATH))
        lock["evaluation"]["cross_model_pooling"] = "allowed"
        with self.assertRaises(MethodLockError):
            validate_method_lock(lock)

    def test_pod_preparer_uses_every_locked_checkpoint_revision(self) -> None:
        lock = load_method_lock(LOCK_PATH)
        script = (LOCK_PATH.parent / "prepare_model_on_pod.sh").read_text(
            encoding="utf-8"
        )
        for model_key in ("qwen", "gemma3-12b"):
            for source in lock["model_matrix"][model_key]["checkpoint_sources"].values():
                self.assertIn(source["repository"], script)
                self.assertIn(source["revision"], script)
        self.assertIn("nla_source_receipt.json", script)

    def test_end_to_end_path_verifies_persistent_models_without_downloading(self) -> None:
        run_script = (LOCK_PATH.parent / "run_model_end_to_end.sh").read_text(
            encoding="utf-8"
        )
        verifier = (LOCK_PATH.parent / "verify_persistent_models.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("/workspace/models", run_script)
        self.assertIn("verify_persistent_models", run_script)
        self.assertNotIn("prepare_model_on_pod.sh", run_script)
        self.assertNotIn("snapshot_download(", run_script)
        self.assertNotIn("snapshot_download(", verifier)
        self.assertIn("files_metadata=True", verifier)

    def test_direction_build_skips_the_behavioral_smoke_explicitly(self) -> None:
        build_script = (LOCK_PATH.parent / "build_on_pod.sh").read_text(
            encoding="utf-8"
        )
        session_script = (LAB / "tools" / "session_start.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("NLA_DIRECTION_BUILD_ONLY=1", build_script)
        self.assertIn('NLA_DIRECTION_BUILD_ONLY:-0', session_script)
        self.assertIn("direction-build GPU preflight (no decoding)", session_script)

    def test_frozen_heldout_prompt_bank_is_valid(self) -> None:
        bank = load_prompt_bank(PROMPT_BANK_PATH)
        self.assertEqual(len(bank["prompts"]), 40)
        self.assertEqual(
            sum(
                row["track"] == "ordinary_optional"
                for row in bank["prompts"]
            ),
            30,
        )

    def test_historical_calibration_and_caa_banks_are_frozen(self) -> None:
        heldout = load_prompt_bank(PROMPT_BANK_PATH)
        calibration = load_yaml(CALIBRATION_PATH)
        caa = load_yaml(CAA_PATH)
        validate_calibration_bank(calibration, heldout=heldout)
        validate_caa_manifest(caa, calibration=calibration, heldout=heldout)
        self.assertEqual(len(calibration["prompts"]), 6)
        self.assertEqual(len(caa["rows"]), 300)
        self.assertEqual(
            calibration["prompts"][0]["text"], "Tell me about your day."
        )

    def test_calibration_cannot_overlap_heldout(self) -> None:
        heldout = load_prompt_bank(PROMPT_BANK_PATH)
        calibration = deepcopy(load_yaml(CALIBRATION_PATH))
        calibration["prompts"][0]["text"] = heldout["prompts"][0]["text"]
        calibration["prompts"][0]["text_sha256"] = heldout["prompts"][0]["input_sha256"]
        with self.assertRaises(DataFreezeError):
            validate_calibration_bank(calibration, heldout=heldout)

    def test_prompt_text_or_target_leakage_fails(self) -> None:
        bank = deepcopy(load_prompt_bank(PROMPT_BANK_PATH))
        bank["prompts"][0]["text"] = (
            "Tell me the latest celebrity news using emoji."
        )
        with self.assertRaises(MethodLockError):
            validate_prompt_bank(bank)


if __name__ == "__main__":
    unittest.main()
