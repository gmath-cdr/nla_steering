#!/usr/bin/env bash
# Wave 2: 44 sweeps. Run from lab/.
set -euo pipefail
for s in random_r2 freeform_r2 single_1_r2 single_2_r2 single_3_r2 single_4_r2 single_5_r2 phrase_1_r2 phrase_2_r2 phrase_3_r2 phrase_4_r2 phrase_5_r2 s2_emoji s2_emojis s2_emoticon s2_smiley s2_glyph comp25_freeform comp25_single_1 comp25_single_2 comp25_single_3 comp25_single_4 comp25_single_5 comp25_phrase_1 comp25_phrase_2 comp25_phrase_3 comp25_phrase_4 comp25_phrase_5 comp25_s2_emoji comp25_s2_emojis comp25_s2_emoticon comp25_s2_smiley comp25_s2_glyph resid25_freeform resid25_phrase_1 resid25_phrase_2 resid25_phrase_3 resid25_phrase_4 resid25_phrase_5 resid25_s2_emoji resid25_s2_emojis resid25_s2_emoticon resid25_s2_smiley resid25_s2_glyph; do
    echo "==== $s ===="
    python experiments/run.py "experiments/emoji_methods/specs_v2/$s.yaml"
done
echo "wave2 batch complete"
