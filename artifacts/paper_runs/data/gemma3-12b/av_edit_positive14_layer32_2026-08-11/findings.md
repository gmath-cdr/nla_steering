# Gemma 3 12B layer-32 prompt-specific AV-edit sweep

This is the corrected layer-32 rerun of the 14 concepts that passed the Qwen
held-out confirmation. The earlier Gemma layer-20 run is not included because
the Gemma AV/AR checkpoints were trained for layer 32.

## Aggregate alpha sweep

| Alpha | AV target hit | Random hit | Prompt coverage | Quality pass | Similarity | Repeated trigrams |
|---:|---:|---:|---:|---:|---:|---:|
| 0.26 | 30.4% | 0.0% | 39.3% | 73.2% | 0.245 | 0.047 |
| 0.28 | 28.6% | 0.0% | 39.3% | 66.1% | 0.200 | 0.088 |
| 0.30 | 35.7% | 0.0% | 42.9% | 51.8% | 0.184 | 0.116 |
| 0.32 | 25.0% | 0.0% | 35.7% | 44.6% | 0.180 | 0.130 |
| 0.34 | 28.6% | 0.0% | 46.4% | 41.1% | 0.158 | 0.114 |
| 0.36 | 28.6% | 0.0% | 35.7% | 35.7% | 0.137 | 0.135 |
| 0.38 | 33.9% | 0.0% | 46.4% | 26.8% | 0.112 | 0.172 |

Alpha 0.26 has the best aggregate quality/effect tradeoff. It narrowly misses
the frozen 75% aggregate quality threshold and does not reach 50% prompt
coverage across all 14 concepts.

## Concept-level findings

Using one fixed alpha (0.26), three concepts pass both the strict causal and
semantic-quality gates:

| ID | Concept | AV hit | Quality pass |
|---:|---|---:|---:|
| 11 | Political and legal context | 75% | 100% |
| 287 | Coding constructs, color attributes, and time-related data | 75% | 100% |
| 359 | Legal terminology and procedural context | 100% | 100% |

Concept 446 also passes cleanly at alpha 0.28, but this is a post-hoc
per-concept alpha choice and is not counted in the fixed-alpha headline.

The evidence supports a real, non-random Gemma steering effect at the correct
layer, but only as a small screen: two prompts and four AV samples per concept.
The three fixed-alpha positives require a larger held-out confirmation before
being treated as robust proofpoints.
