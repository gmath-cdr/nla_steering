from __future__ import annotations

import sys
import unittest
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from nla_lab.generation import (  # noqa: E402
    GENERATION_OUTPUT_SCHEMA,
    generation_output_metadata,
)
from nla_lab.probes import emojis as historical_emoji_codepoints  # noqa: E402
from nla_lab.target_metrics import (  # noqa: E402
    EMOJI_CLUSTER_SCORER_ID,
    emoji_cluster_metrics,
    emoji_cluster_spans,
    score_target,
)


class EmojiClusterMetricTests(unittest.TestCase):
    def test_ascii_emoticons_are_secondary_not_unicode_emoji(self):
        scored = score_target(
            "Clear prose :) and <3, but 12:30 and https://x.test are not faces.",
            {"schema": "target-metric/v1", "kind": "emoji_clusters", "minimum_presence_count": 1},
        )
        self.assertEqual(scored["target_present"], 0.0)
        self.assertEqual(scored["emoji_cluster_count"], 0.0)
        self.assertEqual(scored["ascii_emoticon_count"], 2.0)
        self.assertEqual(scored["ascii_emoticon_present"], 1.0)

    def test_sequences_count_as_clusters_not_component_codepoints(self) -> None:
        woman_technologist = "\U0001f469\U0001f3fd\u200d\U0001f4bb"
        us_flag = "\U0001f1fa\U0001f1f8"
        keycap_one = "1\ufe0f\u20e3"
        red_heart = "\u2764\ufe0f"
        text = f"hello world {woman_technologist} {us_flag} {keycap_one} {red_heart}"

        spans = emoji_cluster_spans(text)
        self.assertEqual([text[start:end] for start, end in spans], [
            woman_technologist, us_flag, keycap_one, red_heart,
        ])
        metrics = emoji_cluster_metrics(text)
        self.assertEqual(metrics["emoji_cluster_scorer"], EMOJI_CLUSTER_SCORER_ID)
        self.assertEqual(metrics["emoji_cluster_count"], 4.0)
        self.assertEqual(metrics["emoji_cluster_present"], 1.0)
        self.assertEqual(metrics["emoji_clusters_per_100_words"], 200.0)
        self.assertAlmostEqual(metrics["emoji_cluster_content_fraction"], 4 / 6)
        self.assertEqual(metrics["max_adjacent_emoji_cluster_run"], 1.0)

    def test_zwj_family_is_one_cluster_and_historical_probe_is_unchanged(self) -> None:
        family = (
            "\U0001f468\u200d\U0001f469\u200d"
            "\U0001f467\u200d\U0001f466"
        )
        self.assertEqual(len(emoji_cluster_spans(family)), 1)
        self.assertEqual(emoji_cluster_metrics(family)["emoji_cluster_count"], 1.0)
        # The old probe remains a code-point counter for historical continuity.
        self.assertEqual(historical_emoji_codepoints(family), 4)

    def test_adjacent_flags_and_emoji_only_output_expose_flood_metrics(self) -> None:
        text = (
            "\U0001f1fa\U0001f1f8"
            "\U0001f1e8\U0001f1e6"
            "\U0001f600"
        )
        metrics = emoji_cluster_metrics(text)
        self.assertEqual(metrics["emoji_cluster_count"], 3.0)
        self.assertEqual(metrics["max_adjacent_emoji_cluster_run"], 3.0)
        self.assertEqual(metrics["emoji_cluster_content_fraction"], 1.0)
        self.assertEqual(metrics["emoji_clusters_per_100_words"], 300.0)

        scored = score_target(text, {"kind": "emoji_clusters"})
        self.assertEqual(scored["target_count"], 3.0)
        self.assertEqual(scored["target_present"], 1.0)
        self.assertEqual(scored["target_rate_per_100_words"], 300.0)
        self.assertEqual(scored["target_token_fraction"], 3.0)

    def test_variation_selectors_do_not_become_separate_clusters(self) -> None:
        copyright_text = "\u00a9"
        copyright_emoji = "\u00a9\ufe0f"
        self.assertEqual(len(emoji_cluster_spans(copyright_text)), 0)
        self.assertEqual(len(emoji_cluster_spans(copyright_emoji)), 1)
        self.assertEqual(len(emoji_cluster_spans("\ufe0f\U0001f3fd :)")), 0)

    def test_presence_threshold_uses_cluster_count(self) -> None:
        scored = score_target(
            "A single \U0001f600 cluster.",
            {"kind": "emoji_clusters", "minimum_presence_count": 2},
        )
        self.assertEqual(scored["target_count"], 1.0)
        self.assertEqual(scored["target_present"], 0.0)
        self.assertEqual(scored["emoji_cluster_present"], 1.0)


class GenerationOutputMetadataTests(unittest.TestCase):
    def test_token_budget_flag_is_exact_and_versioned(self) -> None:
        below = generation_output_metadata(7, 8)
        at_limit = generation_output_metadata(8, 8)
        self.assertEqual(below, {
            "schema": GENERATION_OUTPUT_SCHEMA,
            "output_token_count": 7,
            "max_new_tokens": 8,
            "hit_max_new_tokens": False,
        })
        self.assertTrue(at_limit["hit_max_new_tokens"])

    def test_zero_output_is_valid_but_invalid_counts_fail_closed(self) -> None:
        self.assertFalse(generation_output_metadata(0, 8)["hit_max_new_tokens"])
        for args in ((-1, 8), (9, 8), (0, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                generation_output_metadata(*args)
        with self.assertRaises(TypeError):
            generation_output_metadata(True, 8)


if __name__ == "__main__":
    unittest.main()
