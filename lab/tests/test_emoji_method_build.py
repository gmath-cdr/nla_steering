from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from experiments.emoji_method_comparison.build_directions import (  # noqa: E402
    BUILD_SCHEMA,
    DirectionBuildError,
    _render_caa_prompt,
    _vector_receipt,
    validate_build_report,
)
from nla_lab.study import canonical_sha256  # noqa: E402


class EmojiMethodBuildTests(unittest.TestCase):
    def test_caa_prompt_balance_changes_options_not_question(self) -> None:
        construction = {
            "positive_requirement": "positive",
            "negative_requirement": "negative",
            "prompt_template": "{input}\n(A) {option_a}\n(B) {option_b}",
        }
        a = _render_caa_prompt(
            {"text": "Question?", "positive_option": "A"}, construction
        )
        b = _render_caa_prompt(
            {"text": "Question?", "positive_option": "B"}, construction
        )
        self.assertIn("(A) positive", a)
        self.assertIn("(B) positive", b)
        self.assertTrue(a.startswith("Question?"))
        self.assertTrue(b.startswith("Question?"))

    def test_build_report_binds_every_archive_member(self) -> None:
        profiles = (
            (
                "qwen", "qwen2.5-7b-instruct.resid-post.L20.d3584", 20, 3584,
                {
                    "base": {"repository": "Qwen/Qwen2.5-7B-Instruct", "revision": "a09a35458c702b33eeacc393d103063234e8bc28"},
                    "av": {"repository": "kitft/nla-qwen2.5-7b-L20-av", "revision": "b88469162777ae6553bc14208eb0cb579336f8f4"},
                    "ar": {"repository": "kitft/nla-qwen2.5-7b-L20-ar", "revision": "e2c9e57eac213d37a31612087f645ab6332c1bb6"},
                },
            ),
            (
                "gemma3-12b", "gemma3-12b-it.resid-post.L32.d3840", 32, 3840,
                {
                    "base": {"repository": "google/gemma-3-12b-it", "revision": "96b6f1eccf38110c56df3a15bffe176da04bfd80"},
                    "av": {"repository": "kitft/nla-gemma3-12b-L32-av", "revision": "7aec22599e8a9cd533564868999b443fcc963cf4"},
                    "ar": {"repository": "kitft/nla-gemma3-12b-L32-ar", "revision": "3d6901d8243182d642af9dea452ca91549a94615"},
                },
            ),
        )
        for model_key, space_id, layer, width, sources in profiles:
            with self.subTest(model_key=model_key), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "directions.npz"
                vectors = {
                    name: np.arange(width, dtype=np.float32) + offset + 1
                    for offset, name in enumerate(
                        ("caa", "nla_phrase", "nla_glyph", "jlens_word_atom")
                    )
                }
                np.savez_compressed(path, **vectors)
                checkpoint = {
                    "marker_sha256": {
                        "config.json": "a" * 64,
                        "nla_source_receipt.json": "c" * 64,
                    },
                    "weights_hashed": True,
                    "weight_files": [
                        {"name": "model.safetensors", "bytes": 1, "sha256": "b" * 64}
                    ],
                }
                checkpoints = {}
                for role, source in sources.items():
                    value = dict(checkpoint)
                    value["source"] = source
                    value["snapshot_identity_sha256"] = canonical_sha256({
                        "source": source,
                        "marker_sha256": value["marker_sha256"],
                        "weight_files": value["weight_files"],
                    })
                    checkpoints[role] = value
                report = {
                    "schema": BUILD_SCHEMA,
                    "study_id": "crossmodel-qwen-l20-gemma-l32-caa-nla-emoji-v5",
                    "runtime": {
                        "model_key": model_key,
                        "space_id": space_id,
                        "layer": layer,
                        "hidden_width": width,
                    },
                    "git": {"dirty": False},
                    "checkpoint_receipts": checkpoints,
                    "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "directions": {
                        name: _vector_receipt(vector, {})
                        for name, vector in vectors.items()
                    },
                }
                report["receipt_sha256"] = canonical_sha256(report)
                validate_build_report(report, path)
                report["directions"]["caa"]["unit_direction_sha256"] = "0" * 64
                with self.assertRaises(DirectionBuildError):
                    validate_build_report(report, path)


if __name__ == "__main__":
    unittest.main()
