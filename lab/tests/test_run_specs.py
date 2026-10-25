from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

from run_specs import _completed_result, _expand, _load_rank  # noqa: E402


class BatchSpecTests(unittest.TestCase):
    def test_inline_directions_are_ranked_before_lightweight_arms(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inline = root / "inline.yaml"
            file_vector = root / "file.yaml"
            random = root / "random.yaml"
            baseline = root / "baseline.yaml"
            inline.write_text("direction:\n  positive: yes\n  negative: no\n")
            file_vector.write_text("direction_file: vectors.npz#target\n")
            random.write_text("direction: random\n")
            baseline.write_text("name: baseline\n")
            ranked = sorted(
                [baseline, random, file_vector, inline], key=_load_rank
            )
            self.assertEqual(
                ranked, [inline, file_vector, random, baseline]
            )

    def test_directory_expansion_excludes_materialization_manifest(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.yaml").write_text("name: one\n")
            (root / "_materialization-qwen.yaml").write_text("schema: manifest\n")
            self.assertEqual(_expand([root]), [(root / "one.yaml").resolve()])

    def test_resume_requires_exact_integrity_and_cardinality(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "spec.yaml"
            spec_path.write_text(
                "name: test-arm\nexpected_records: 2\n"
                "spec_integrity:\n  sha256: abc\n"
            )
            result_path = root / "test-arm_20260821-000000.json"
            result_path.write_text(
                '{"spec":{"spec_integrity":{"sha256":"abc"}},'
                '"records":[{},{}]}'
            )
            self.assertEqual(_completed_result(spec_path, root), result_path)
            result_path.write_text(
                '{"spec":{"spec_integrity":{"sha256":"different"}},'
                '"records":[{},{}]}'
            )
            with self.assertRaises(ValueError):
                _completed_result(spec_path, root)


if __name__ == "__main__":
    unittest.main()
