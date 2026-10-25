from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path

import yaml


LAB = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "experiment_manifest_tested", LAB / "analysis" / "build_experiment_manifest.py"
)
assert SPEC is not None and SPEC.loader is not None
MANIFEST_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST_MODULE)


class ExperimentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = MANIFEST_MODULE.build_manifest()
        cls.entries = {
            entry["experiment_name"]: entry for entry in cls.manifest["entries"]
        }

    def test_complete_corpus_and_known_counts(self) -> None:
        summary = self.manifest["summary"]
        self.assertEqual(summary["run_count"], 137)
        self.assertEqual(summary["record_count"], 37271)
        self.assertEqual(summary["unresolved_count"], 0)
        self.assertEqual(summary["root_method_counts"], {
            "nla_ar_contrast": 59,
            "nla_av_edit_ar": 59,
            "none": 7,
            "random_isotropic": 12,
        })
        self.assertEqual(summary["file_vector_run_count"], 82)
        self.assertEqual(summary["verified_vector_member_run_count"], 82)

    def test_saved_vector_archives_and_members_are_hashed(self) -> None:
        saved = [entry for entry in self.manifest["entries"] if entry["vector_file"]]
        self.assertEqual(len(saved), 82)
        for entry in saved:
            self.assertEqual(
                entry["vector_integrity_status"],
                "archive_and_member_verified",
            )
            self.assertEqual(len(entry["vector_artifact_sha256"]), 64)
            self.assertEqual(len(entry["vector_member_sha256"]), 64)
            self.assertGreater(entry["vector_artifact_bytes"], 0)

    def test_wordle_semantics_and_exact_house_are_ar_contrasts(self) -> None:
        for name in (
            "wcrank-verb", "wcrank-semantic", "wordle2-ff-dwell",
            "gcore-g-dwell", "gcore-g-lion", "gcore-g-len-seven",
        ):
            self.assertEqual(self.entries[name]["root_method"], "nla_ar_contrast")
            self.assertEqual(self.entries[name]["output_role"], "whole")

    def test_canonical_av_edit_evidence_is_style_family(self) -> None:
        for name in (
            "emojimx-single-1", "emojimx-phrase-1", "emojimx2-s2-emoji",
            "gcore-g-emoji", "gcore-g-phrase-glyph",
        ):
            self.assertEqual(self.entries[name]["root_method"], "nla_av_edit_ar")

    def test_no_current_caa_or_pure_j_atom(self) -> None:
        roots = {entry["root_method"] for entry in self.manifest["entries"]}
        self.assertNotIn("caa_mean_activation_contrast", roots)
        self.assertNotIn("j_lens_atom", roots)

    def test_registered_artifact_hashes_match_files(self) -> None:
        registry = yaml.safe_load(
            (LAB / "registry" / "artifacts.yaml").read_text(encoding="utf-8")
        )
        repo = LAB.parent
        for artifact in registry["artifacts"]:
            path = repo / artifact["path"]
            if artifact.get("availability") == "local_optional" and not path.exists():
                continue
            self.assertTrue(path.exists(), artifact["path"])
            self.assertEqual(path.stat().st_size, artifact["bytes"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, artifact["sha256"])


if __name__ == "__main__":
    unittest.main()
