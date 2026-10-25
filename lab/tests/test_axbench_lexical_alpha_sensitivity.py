from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

import yaml


LAB = Path(__file__).resolve().parents[1]
REPO = LAB.parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.axbench_lexicality20.lexical_gen_specs import (  # noqa: E402
    _cell_id,
    _cell_payload,
    build_materialization,
    validate_manifest,
)
from experiments.axbench_lexicality20.lexical_select_alpha import (  # noqa: E402
    LexicalSelectionError,
    build_receipt,
    choose,
    exact_matches,
    validate_receipt,
)


ARTIFACT_ROOT = (
    REPO / "artifacts" / "paper_runs" / "data" / "outcome_blind_axb44"
)
LEXICONS = ARTIFACT_ROOT / "strict_lexical" / "blind10_lexicons_v2.json"
ARCHIVES = {
    "qwen": ARTIFACT_ROOT / "archives" / "axb44-688f4c1-qwen-complete.tar.gz",
    "gemma": ARTIFACT_ROOT / "archives" / "axb44-688f4c1-gemma-complete.tar.gz",
}
RUN_NAMES = {"qwen": "qwen-run-02", "gemma": "gemma-run-02"}


def safe_extract(archive: Path, target: Path) -> Path:
    root = target.resolve()
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        for member in members:
            candidate = (root / member.name).resolve()
            if not candidate.is_relative_to(root):
                raise ValueError("unsafe archive member")
            if not (member.isfile() or member.isdir()):
                raise ValueError("archive contains a non-file/directory member")
        try:
            stream.extractall(target, members=members, filter="data")
        except TypeError:
            stream.extractall(target, members=members)
    return target


class LexicalAlphaSensitivityUnitTests(unittest.TestCase):
    def test_exact_matcher_preserves_whole_word_boundaries(self) -> None:
        terms = ["real", "real estate", "property"]
        self.assertTrue(exact_matches("REAL estate and property", terms))
        self.assertFalse(exact_matches("surreal and propertyless", terms))
        self.assertTrue(exact_matches("a real-world example", terms))

    def test_selector_uses_mean_hit_and_smaller_alpha_for_all_ties(self) -> None:
        rows = [
            {
                "alpha": alpha,
                "text": text,
                "concept_id": 20,
                "arm_id": "nla",
                "prompt_id": f"p{index}",
                "generation_seed": index,
                "result_sha256": "a" * 64,
                "record_index": index,
            }
            for alpha, texts in (
                (0.1, ["none", "none", "none", "none"]),
                (0.2, ["target", "target", "none", "none"]),
                (0.3, ["target", "target", "none", "none"]),
            )
            for index, text in enumerate(texts)
        ]
        selected, details, all_zero = choose(rows, [0.1, 0.2, 0.3], ["target"])
        self.assertEqual(selected, 0.2)
        self.assertFalse(all_zero)
        self.assertEqual([row["hits"] for row in details], [0, 2, 2])

        zero_rows = [
            {
                "alpha": alpha,
                "text": "none",
                "concept_id": 20,
                "arm_id": "nla",
                "prompt_id": f"p{index}",
                "generation_seed": index,
                "result_sha256": "a" * 64,
                "record_index": index,
            }
            for alpha in (0.1, 0.2)
            for index in range(4)
        ]
        selected, _, all_zero = choose(zero_rows, [0.1, 0.2], ["target"])
        self.assertEqual(selected, 0.1)
        self.assertTrue(all_zero)

    def test_cell_identity_includes_mode_and_random_identity(self) -> None:
        base = dict(
            model_key="qwen",
            kind="random",
            concept_id=20,
            prompt_id="c20--alpaca-0041",
            generation_seed=123,
            alpha=0.2,
            mode="all",
            control_seed=17,
        )
        first = _cell_id(_cell_payload(**base))
        changed_mode = _cell_id(_cell_payload(**{**base, "mode": "last_and_decode"}))
        changed_seed = _cell_id(_cell_payload(**{**base, "control_seed": 42}))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed_mode)
        self.assertNotEqual(first, changed_seed)

    def test_receipt_and_manifest_tampering_fails(self) -> None:
        receipt = {
            "schema": "axb44-lexical-alpha-receipt/v1",
            "status": "sealed",
            "analysis_id": "axb44-lexical-alpha-sensitivity-v1",
            "campaign_id": "axb44-crossmodel-v1",
            "review_payload_sha256": (
                "0fd7075c011d08192185a8fa60603942eef7458ebadf03bbb0f5974edf85cd56"
            ),
            "model_key": "qwen",
            "held_out_response_text_read_during_selection": False,
            "chosen_alpha": {
                str(concept_id): {"diffmean": 0.1, "caa": 0.1, "nla": 0.1}
                for concept_id in range(10)
            },
        }
        from experiments.axbench_lexicality20.protocol import canonical_sha256

        receipt["integrity_sha256"] = canonical_sha256(receipt)
        validate_receipt(receipt, model_key="qwen")
        tampered = deepcopy(receipt)
        tampered["chosen_alpha"]["0"]["nla"] = 0.2
        with self.assertRaises(LexicalSelectionError):
            validate_receipt(tampered, model_key="qwen")


@unittest.skipUnless(all(path.is_file() for path in ARCHIVES.values()), "paper archives absent")
class LexicalAlphaSensitivityArchiveTests(unittest.TestCase):
    def test_authenticated_archives_reproduce_receipts_and_delta_counts(self) -> None:
        expected = {
            "qwen": {"changed": 18, "all_zero": 8, "new": 528, "treatment": 144, "random": 384},
            "gemma": {"changed": 20, "all_zero": 7, "new": 568, "treatment": 160, "random": 408},
        }
        with tempfile.TemporaryDirectory(prefix="axb44-lexical-alpha-test-") as temporary:
            temp = Path(temporary)
            for model_key in ("qwen", "gemma"):
                extracted = safe_extract(ARCHIVES[model_key], temp / model_key)
                run_root = extracted / RUN_NAMES[model_key]
                receipt = build_receipt(
                    model_key=model_key,
                    run_root=run_root,
                    lexicons_path=LEXICONS,
                )
                validate_receipt(receipt, model_key=model_key)
                self.assertEqual(receipt["changed_choice_count"], expected[model_key]["changed"])
                self.assertEqual(receipt["all_zero_group_count"], expected[model_key]["all_zero"])

                receipt_path = temp / f"{model_key}.receipt.json"
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
                manifest = build_materialization(
                    model_key=model_key,
                    run_root=run_root,
                    receipt_path=receipt_path,
                    output_dir=temp / f"{model_key}-specs",
                    manifest_path=temp / f"{model_key}.manifest.json",
                )
                validate_manifest(manifest, model_key=model_key)
                self.assertEqual(manifest["counts"]["new_rows"], expected[model_key]["new"])
                self.assertEqual(
                    manifest["counts"]["new_treatment_rows"], expected[model_key]["treatment"]
                )
                self.assertEqual(
                    manifest["counts"]["new_random_rows"], expected[model_key]["random"]
                )
                historical_randoms = manifest["source_run"]["random_direction_unit_sha256"]
                self.assertEqual(len(historical_randoms), 120)
                random_specs = 0
                for spec_path in (temp / f"{model_key}-specs").glob("*.yaml"):
                    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                    if spec.get("direction") != "random":
                        continue
                    random_specs += 1
                    control_seed = int(spec["study"]["arm_id"].split("_")[1])
                    concept_id = int(spec["prompts"][0]["concept_id"])
                    prompt_id = str(spec["prompts"][0]["id"])
                    historical_key = f"c{concept_id}|{prompt_id}|seed{control_seed}"
                    self.assertEqual(
                        spec["expected_direction_integrity"]["unit_direction_sha256"],
                        historical_randoms[historical_key],
                    )
                self.assertEqual(
                    random_specs,
                    expected[model_key]["random"] // 2,
                )


if __name__ == "__main__":
    unittest.main()
