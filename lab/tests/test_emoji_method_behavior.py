from __future__ import annotations

import sys
from pathlib import Path
import hashlib
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_method_comparison.analysis import (  # noqa: E402
    evaluate_heldout,
    make_alpha_receipt,
    select_one_alpha,
)
from experiments.emoji_method_comparison.harness import (  # noqa: E402
    PRIMARY_IDS,
    build_calibration_specs,
    build_heldout_specs,
    expected_heldout_counts,
    load_bundle,
)
from nla_lab.methods import resolve_method  # noqa: E402
from nla_lab.study import canonical_sha256, seal_alpha_receipt  # noqa: E402
from experiments.emoji_method_comparison.verify_persistent_models import (  # noqa: E402
    PersistentModelError,
    verify_one,
)


def fake_build(model_key: str) -> dict:
    profile = load_bundle()["lock"]["model_matrix"][model_key]
    width = profile["hidden_width"]
    directions = {}
    for index, arm in enumerate((*PRIMARY_IDS, "jlens_word_atom")):
        directions[arm] = {
            "hidden_width": width,
            "raw_norm": 1.0 + index,
            "source_member_f32_sha256": f"{index + 1:064x}",
            "unit_direction_sha256": f"{index + 11:064x}",
            "details": {},
        }
    directions["jlens_word_atom"]["details"] = {
        "token": {"surface": " emoji", "token_id": 1234},
        "unembedding_row_f32_sha256": "a" * 64,
        "effective_norm_response_f32_sha256": "b" * 64,
    }
    marker = {"config.json": "c" * 64, "tokenizer_config.json": "d" * 64}
    return {
        "archive": Path("/workspace/fake/directions.npz"),
        "report_file_sha256": "e" * 64,
        "report": {
            "receipt_sha256": "f" * 64,
            "archive_sha256": "1" * 64,
            "runtime": {"model_key": model_key, "space_id": profile["space_id"]},
            "checkpoint_receipts": {"base": {"marker_sha256": marker}},
            "directions": directions,
        },
    }


def safe_metrics(present: float) -> dict:
    return {
        "target_present": present,
        "unicode_word_count": 30.0,
        "unique_unicode_word_ratio": 0.8,
        "max_token_run": 1.0,
        "dominant_token_fraction": 0.1,
        "max_character_run": 2.0,
        "replacement_character_count": 0.0,
        "emoji_cluster_count": present,
        "emoji_cluster_to_word_fraction": present / 30.0,
    }


class EmojiMethodBehaviorHarnessTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle()

    def test_calibration_specs_are_model_specific_and_complete(self):
        for model_key, expected_alphas in (
            ("qwen", 7), ("gemma3-12b", 5),
        ):
            specs = build_calibration_specs(
                self.bundle, fake_build(model_key), model_key=model_key
            )
            self.assertEqual(len(specs), 3)
            self.assertEqual({spec["study"]["arm_id"] for spec in specs}, set(PRIMARY_IDS))
            self.assertTrue(all(len(spec["alphas"]) == expected_alphas for spec in specs))
            self.assertTrue(all(spec["mode"] == "decode" for spec in specs))
            self.assertTrue(all(spec["expected_records"] == 6 * 3 * expected_alphas for spec in specs))
            roots = {spec["study"]["arm_id"]: resolve_method(spec)["root"]["method"] for spec in specs}
            self.assertEqual(roots["caa"], "caa_mean_activation_contrast")
            self.assertEqual(roots["nla_phrase"], "nla_av_edit_ar")
            self.assertNotIn("jlens_word_atom", roots)

    def test_heldout_is_64_resumable_chunks_and_3200_records(self):
        build = fake_build("qwen")
        calibration_specs = build_calibration_specs(self.bundle, build, model_key="qwen")
        chosen = {arm: 0.1 * (index + 1) for index, arm in enumerate(PRIMARY_IDS)}
        receipt = seal_alpha_receipt({
            "schema": "axbench-alpha-receipt/v1",
            "status": "frozen",
            "analysis": "emoji-method-comparison-paired-calibration/v1",
            "selection_rule": "smallest-safe-alpha-within-locked-near-best/v1",
            "model_key": "qwen", "concept_key": "emoji",
            "chosen_alpha_by_arm": chosen,
            "random_control_alphas": sorted(set(chosen.values()), key=abs),
            "selection_details": {}, "calibration_result_sha256": {},
            "lock_bundle_sha256": self.bundle["lock_bundle_sha256"],
            "base_checkpoint_marker_sha256": calibration_specs[0]["expected_base_checkpoint_marker_sha256"],
            "treatment_direction_integrity": {
                spec["study"]["arm_id"]: dict(spec["expected_direction_integrity"])
                for spec in calibration_specs
            },
            "treatment_exclusions": {}, "calibration_git_commit": "commit",
            "calibration_package_versions": {"torch": "test"},
            "created_utc": "2026-08-24T00:00:00Z",
            "audit": {"reviewer": "test", "note": "test fixture", "frozen_utc": "2026-08-24T00:00:00Z"},
        })
        specs = build_heldout_specs(
            self.bundle, build, model_key="qwen", alpha_receipt=receipt
        )
        self.assertEqual(len(specs), 64)
        self.assertEqual(sum(spec["expected_records"] for spec in specs), 3200)
        self.assertEqual(len({spec["study"]["chunk_id"] for spec in specs}), 8)
        explicit = [spec for spec in specs if spec["study"]["arm_id"] == "explicit_prompt"]
        self.assertTrue(all(spec["alphas"] == [0.0] for spec in explicit))
        self.assertTrue(all(
            prompt["text"].endswith("Use context-appropriate Unicode emojis and emoticons in your response.")
            for spec in explicit for prompt in spec["prompts"]
        ))
        randoms = [spec for spec in specs if spec["study"]["arm_id"].startswith("random_")]
        self.assertTrue(all(spec["alphas"] == [0.0, 0.1, 0.2, 0.30000000000000004] for spec in randoms))

    def test_calibration_selection_uses_paired_prompts_and_smallest_near_best(self):
        spec = build_calibration_specs(
            self.bundle, fake_build("qwen"), model_key="qwen"
        )[0]
        records = []
        for prompt_index, prompt in enumerate(spec["prompts"]):
            for sample_index in range(spec["samples_per_prompt"]):
                seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                for alpha in spec["alphas"]:
                    present = float(alpha >= 0.2)
                    records.append({
                        "prompt_id": prompt["id"], "prompt_split": "calibration",
                        "sample": seed, "alpha": alpha, "text": "ordinary safe prose",
                        "target_metrics": safe_metrics(present),
                        "generation": {"hit_max_new_tokens": False},
                    })
        result = {"spec": spec, "records": records}
        selected = select_one_alpha(result, self.bundle)
        self.assertEqual(selected["chosen_alpha"], 0.2)
        self.assertEqual(selected["best_safe_effect"], 1.0)

    def test_calibration_with_no_safe_alpha_returns_frozen_exclusion_decision(self):
        spec = build_calibration_specs(
            self.bundle, fake_build("qwen"), model_key="qwen"
        )[0]
        records = []
        for prompt_index, prompt in enumerate(spec["prompts"]):
            for sample_index in range(spec["samples_per_prompt"]):
                seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                for alpha in spec["alphas"]:
                    metrics = safe_metrics(float(alpha > 0))
                    if alpha > 0:
                        metrics["unique_unicode_word_ratio"] = 0.0
                    records.append({
                        "prompt_id": prompt["id"], "prompt_split": "calibration",
                        "sample": seed, "alpha": alpha, "text": "otherwise coherent prose",
                        "target_metrics": metrics,
                        "generation": {"hit_max_new_tokens": False},
                    })
        selected = select_one_alpha({"spec": spec, "records": records}, self.bundle)
        self.assertEqual(selected["status"], "excluded_no_safe_alpha")
        self.assertIsNone(selected["chosen_alpha"])
        self.assertTrue(all(not row["passes_safety"] for row in selected["candidates"]))

    def test_fixed_ceiling_is_reported_but_not_an_automatic_safety_failure(self):
        spec = build_calibration_specs(
            self.bundle, fake_build("qwen"), model_key="qwen"
        )[0]
        records = []
        for prompt_index, prompt in enumerate(spec["prompts"]):
            for sample_index in range(spec["samples_per_prompt"]):
                seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                for alpha in spec["alphas"]:
                    records.append({
                        "prompt_id": prompt["id"], "prompt_split": "calibration",
                        "sample": seed, "alpha": alpha, "text": "coherent capped prose",
                        "target_metrics": safe_metrics(float(alpha >= 0.2)),
                        "generation": {"hit_max_new_tokens": True},
                    })
        selected = select_one_alpha({"spec": spec, "records": records}, self.bundle)
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["chosen_alpha"], 0.2)
        self.assertTrue(all(row["passes_safety"] for row in selected["candidates"]))

    def test_heldout_cardinality_tracks_one_preregistered_exclusion(self):
        build = fake_build("qwen")
        calibration_specs = build_calibration_specs(self.bundle, build, model_key="qwen")
        chosen = {
            arm: 0.1 * (index + 1)
            for index, arm in enumerate(PRIMARY_IDS)
            if arm != "caa"
        }
        source = {
            "schema": "emoji-method-comparison-treatment-exclusion/v1",
            "study_id": self.bundle["lock"]["study_id"],
            "model_key": "qwen", "arm_id": "caa",
            "reason": "no_nonzero_alpha_passed_frozen_safety_gate",
        }
        receipt = seal_alpha_receipt({
            "schema": "axbench-alpha-receipt/v1", "status": "frozen",
            "analysis": "emoji-method-comparison-paired-calibration/v1",
            "selection_rule": "smallest-safe-alpha-within-locked-near-best/v1",
            "model_key": "qwen", "concept_key": "emoji",
            "chosen_alpha_by_arm": chosen,
            "random_control_alphas": sorted(set(chosen.values()), key=abs),
            "selection_details": {}, "calibration_result_sha256": {},
            "lock_bundle_sha256": self.bundle["lock_bundle_sha256"],
            "base_checkpoint_marker_sha256": calibration_specs[0]["expected_base_checkpoint_marker_sha256"],
            "treatment_direction_integrity": {
                spec["study"]["arm_id"]: dict(spec["expected_direction_integrity"])
                for spec in calibration_specs
            },
            "treatment_exclusions": {
                "caa": {"receipt": source, "receipt_sha256": canonical_sha256(source)}
            },
            "calibration_git_commit": "commit",
            "calibration_package_versions": {"torch": "test"},
            "created_utc": "2026-08-24T00:00:00Z",
            "audit": {"reviewer": "test", "note": "test fixture", "frozen_utc": "2026-08-24T00:00:00Z"},
        })
        specs = build_heldout_specs(
            self.bundle, build, model_key="qwen", alpha_receipt=receipt
        )
        expected = expected_heldout_counts(self.bundle, receipt)
        self.assertEqual(expected, {"result_files": 56, "records": 2400})
        self.assertEqual(len(specs), expected["result_files"])
        self.assertEqual(sum(spec["expected_records"] for spec in specs), expected["records"])
        self.assertNotIn("caa", {spec["study"]["arm_id"] for spec in specs})
        results = []
        for spec in specs:
            arm = spec["study"]["arm_id"]
            records = []
            for prompt_index, prompt in enumerate(spec["prompts"]):
                for sample_index in range(spec["samples_per_prompt"]):
                    seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                    for alpha in spec["alphas"]:
                        active = (
                            arm in chosen
                            and float(alpha) == chosen[arm]
                            and prompt["split"] == "ordinary_optional"
                        ) or arm == "explicit_prompt"
                        records.append({
                            "prompt_id": prompt["id"], "prompt_split": prompt["split"],
                            "sample": seed, "alpha": alpha, "prompt": prompt["text"],
                            "text": "same deterministic baseline" if float(alpha) == 0 and arm != "explicit_prompt" else f"response {arm} {active}",
                            "target_metrics": safe_metrics(float(active)),
                            "generation": {"hit_max_new_tokens": False},
                        })
            integrity = (
                dict(spec["expected_direction_integrity"])
                if arm in chosen else
                {"unit_direction_sha256": f"{arm}-fixed-hash"}
                if arm.startswith("random_") else None
            )
            results.append({
                "spec": spec, "records": records,
                "runtime": {
                    "model_key": "qwen", "git": {"dirty": False},
                    "package_versions": {"torch": "test"},
                    "checkpoint_marker_sha256": {
                        "base": receipt["base_checkpoint_marker_sha256"]
                    },
                },
                "direction_integrity": integrity,
            })
        summary = evaluate_heldout(results, self.bundle, model_key="qwen")
        self.assertEqual(summary["raw_result_files"], 56)
        self.assertEqual(summary["raw_records"], 2400)
        self.assertEqual(set(summary["claims"]), set(chosen))
        self.assertEqual(set(summary["treatment_exclusions"]), {"caa"})

    def test_alpha_receipt_hashes_and_carries_one_machine_exclusion(self):
        specs = build_calibration_specs(
            self.bundle, fake_build("qwen"), model_key="qwen"
        )
        results = []
        for arm_index, spec in enumerate(specs):
            arm = spec["study"]["arm_id"]
            records = []
            for prompt_index, prompt in enumerate(spec["prompts"]):
                for sample_index in range(spec["samples_per_prompt"]):
                    seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                    for alpha in spec["alphas"]:
                        metrics = safe_metrics(float(alpha >= 0.2))
                        if arm == "caa" and alpha > 0:
                            metrics["unique_unicode_word_ratio"] = 0.0
                        records.append({
                            "prompt_id": prompt["id"], "prompt_split": "calibration",
                            "sample": seed, "alpha": alpha, "text": "coherent fixture",
                            "target_metrics": metrics,
                            "generation": {"hit_max_new_tokens": False},
                        })
            results.append({
                "_sha256": f"{arm_index + 1:064x}",
                "spec": spec,
                "method": resolve_method(spec),
                "records": records,
                "runtime": {
                    "model_key": "qwen", "git": {"dirty": False, "commit": "same-commit"},
                    "package_versions": {"torch": "test"},
                    "checkpoint_marker_sha256": {
                        "base": spec["expected_base_checkpoint_marker_sha256"]
                    },
                },
                "direction_integrity": dict(spec["expected_direction_integrity"]),
            })
        receipt = make_alpha_receipt(results, self.bundle, model_key="qwen")
        self.assertEqual(set(receipt["chosen_alpha_by_arm"]), set(PRIMARY_IDS) - {"caa"})
        self.assertEqual(set(receipt["treatment_exclusions"]), {"caa"})
        exclusion = receipt["treatment_exclusions"]["caa"]
        self.assertEqual(exclusion["receipt_sha256"], canonical_sha256(exclusion["receipt"]))

    def test_synthetic_heldout_family_is_complete_paired_and_track_stratified(self):
        build = fake_build("qwen")
        calibration_specs = build_calibration_specs(self.bundle, build, model_key="qwen")
        chosen = {arm: 0.1 * (index + 1) for index, arm in enumerate(PRIMARY_IDS)}
        receipt = seal_alpha_receipt({
            "schema": "axbench-alpha-receipt/v1", "status": "frozen",
            "analysis": "emoji-method-comparison-paired-calibration/v1",
            "selection_rule": "smallest-safe-alpha-within-locked-near-best/v1",
            "model_key": "qwen", "concept_key": "emoji",
            "chosen_alpha_by_arm": chosen,
            "random_control_alphas": sorted(set(chosen.values()), key=abs),
            "selection_details": {}, "calibration_result_sha256": {},
            "lock_bundle_sha256": self.bundle["lock_bundle_sha256"],
            "base_checkpoint_marker_sha256": calibration_specs[0]["expected_base_checkpoint_marker_sha256"],
            "treatment_direction_integrity": {
                spec["study"]["arm_id"]: dict(spec["expected_direction_integrity"])
                for spec in calibration_specs
            },
            "treatment_exclusions": {}, "calibration_git_commit": "commit",
            "calibration_package_versions": {"torch": "test"},
            "created_utc": "2026-08-24T00:00:00Z",
            "audit": {"reviewer": "test", "note": "test fixture", "frozen_utc": "2026-08-24T00:00:00Z"},
        })
        specs = build_heldout_specs(
            self.bundle, build, model_key="qwen", alpha_receipt=receipt
        )
        results = []
        for spec in specs:
            arm = spec["study"]["arm_id"]
            records = []
            for prompt_index, prompt in enumerate(spec["prompts"]):
                for sample_index in range(spec["samples_per_prompt"]):
                    seed = spec["seed_offset"] + prompt_index * spec["samples_per_prompt"] + sample_index
                    for alpha in spec["alphas"]:
                        active = (
                            arm in PRIMARY_IDS
                            and float(alpha) == chosen[arm]
                            and prompt["split"] == "ordinary_optional"
                        ) or arm == "explicit_prompt"
                        records.append({
                            "prompt_id": prompt["id"], "prompt_split": prompt["split"],
                            "sample": seed, "alpha": alpha, "prompt": prompt["text"],
                            "text": "same deterministic baseline" if float(alpha) == 0 and arm != "explicit_prompt" else f"response {arm} {active}",
                            "target_metrics": safe_metrics(float(active)),
                            "generation": {"hit_max_new_tokens": False},
                        })
            if arm in PRIMARY_IDS:
                direction_integrity = dict(spec["expected_direction_integrity"])
            elif arm.startswith("random_"):
                direction_integrity = {"unit_direction_sha256": f"{arm}-fixed-hash"}
            else:
                direction_integrity = None
            results.append({
                "spec": spec, "records": records,
                "runtime": {
                    "model_key": "qwen", "git": {"dirty": False},
                    "package_versions": {"torch": "test"},
                    "checkpoint_marker_sha256": {
                        "base": receipt["base_checkpoint_marker_sha256"]
                    },
                },
                "direction_integrity": direction_integrity,
            })
        summary = evaluate_heldout(results, self.bundle, model_key="qwen")
        self.assertEqual(summary["raw_result_files"], 64)
        self.assertEqual(summary["raw_records"], 3200)
        self.assertTrue(summary["alpha_zero_match_across_hidden_arms"])
        self.assertTrue(all(
            row["statistical_and_deterministic_success"]
            for row in summary["claims"].values()
        ))
        self.assertTrue(all(
            row["tracks"]["formal_technical_transfer"]["effect_vs_alpha_zero"] == 0
            for row in summary["claims"].values()
        ))

    def test_persistent_model_verifier_uses_metadata_and_never_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weight = root / "model.safetensors"
            config = root / "config.json"
            weight.write_bytes(b"frozen-weight-bytes")
            config.write_bytes(b'{"model_type":"fixture"}\n')
            weight_sha = hashlib.sha256(weight.read_bytes()).hexdigest()
            config_bytes = config.read_bytes()
            config_blob = hashlib.sha1(  # noqa: S324 - Git fixture identity
                f"blob {len(config_bytes)}\0".encode() + config_bytes
            ).hexdigest()
            siblings = [
                SimpleNamespace(
                    rfilename="model.safetensors", size=weight.stat().st_size,
                    lfs=SimpleNamespace(sha256=weight_sha), blob_id=None,
                ),
                SimpleNamespace(
                    rfilename="config.json", size=config.stat().st_size,
                    lfs=None, blob_id=config_blob,
                ),
            ]

            class FakeApi:
                def model_info(self, repository, revision, files_metadata, token):
                    self.call = (repository, revision, files_metadata, token)
                    return SimpleNamespace(sha=revision, siblings=siblings)

            module = ModuleType("huggingface_hub")
            module.HfApi = FakeApi
            source = {"repository": "fixture/model", "revision": "a" * 40}
            with patch.dict(sys.modules, {"huggingface_hub": module}):
                report = verify_one(root, source, token="private-test-token")
            self.assertEqual(report["verified_weight_files"], 1)
            self.assertEqual(report["verified_files"], 2)
            self.assertEqual(
                (root / "nla_source_receipt.json").read_text(encoding="utf-8"),
                '{"repository": "fixture/model", "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
            )

            weight.write_bytes(b"tampered")
            (root / "nla_source_receipt.json").unlink()
            with patch.dict(sys.modules, {"huggingface_hub": module}):
                with self.assertRaises(PersistentModelError):
                    verify_one(root, source, token=None)
            self.assertFalse((root / "nla_source_receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
