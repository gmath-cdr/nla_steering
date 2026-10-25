from __future__ import annotations

import unittest

from experiments.axbench_lexicality20.analyze_results import (
    AnalysisError, holm_adjust, multilevel_bootstrap, paired_rows,
    random_contrast,
)


def fixture_rows() -> list[dict]:
    rows = []
    for concept in range(10):
        for prompt_index in range(4):
            track = "concept500_negative" if prompt_index < 2 else "shared_alpaca_transfer"
            prompt_id = f"c{concept}--p{prompt_index}"
            for generation_seed in (100, 200):
                common = {
                    "concept_id": concept, "prompt_id": prompt_id,
                    "generation_seed": generation_seed, "prompt_track": track,
                    "alpha": 0.2, "mode": "last_and_decode",
                }
                rows.append({**common, "arm_id": "nla", "axbench_harmonic": 1.5})
                rows.append({**common, "arm_id": "diffmean", "axbench_harmonic": 1.0})
                for control_seed in (17, 42, 101):
                    rows.append({
                        **common,
                        "arm_id": f"random_{control_seed}_last_and_decode_a0p2",
                        "axbench_harmonic": 0.5,
                    })
    return rows


class AxB44AnalysisTests(unittest.TestCase):
    def test_holm_is_monotone_in_rank(self) -> None:
        adjusted = holm_adjust({"a": 0.01, "b": 0.03, "c": 0.04})
        self.assertEqual(adjusted, {"a": 0.03, "b": 0.06, "c": 0.06})

    def test_multilevel_bootstrap_is_deterministic(self) -> None:
        rows = [
            {"concept_id": concept, "prompt_id": f"p{prompt}", "difference": 0.5}
            for concept in range(10) for prompt in range(4)
        ]
        first = multilevel_bootstrap(rows, seed=17, repetitions=200)
        second = multilevel_bootstrap(rows, seed=17, repetitions=200)
        self.assertEqual(first, second)
        self.assertEqual(first["ci95_low"], 0.5)
        self.assertEqual(first["ci95_high"], 0.5)

    def test_exact_pairing_keeps_tracks_separate(self) -> None:
        rows = fixture_rows()
        union = paired_rows(rows, "nla", "diffmean", track="balanced_union")
        negative = paired_rows(rows, "nla", "diffmean", track="concept500_negative")
        transfer = paired_rows(rows, "nla", "diffmean", track="shared_alpaca_transfer")
        self.assertEqual((len(union), len(negative), len(transfer)), (80, 40, 40))
        self.assertTrue(all(row["difference"] == 0.5 for row in union))

    def test_each_random_seed_is_matched_not_selected(self) -> None:
        rows = fixture_rows()
        for index, control_seed in enumerate((17, 42, 101)):
            result = random_contrast(
                rows, "nla", control_seed, track="balanced_union",
                seed=100 + index,
            )
            self.assertEqual(result["n_pairs"], 80)
            self.assertEqual(result["mean_difference"], 1.0)

    def test_missing_pair_fails_closed(self) -> None:
        rows = fixture_rows()
        rows.pop()
        with self.assertRaises(AnalysisError):
            random_contrast(rows, "nla", 101, track="balanced_union", seed=1)


if __name__ == "__main__":
    unittest.main()
