# Results ledger

## Question

Does the word-score ordering on the ten concepts chosen before steering change
when alpha is selected by calibration concept-word hits rather than by the
AxBench harmonic score H?

## Held-out result

Here, **H-tuned** names the rule used to select alpha. Every number in the
table is still a held-out concept-word rate.

| Model | Method | H-tuned alpha | Word-tuned alpha |
|---|---:|---:|---:|
| Qwen2.5-7B | Unsteered | 1.25% | 1.25% |
| Qwen2.5-7B | Calibrated CAA | 0.00% | 1.25% |
| Qwen2.5-7B | DiffMean | 26.25% | 43.75% |
| Qwen2.5-7B | NLA steering | 22.50% | 47.50% |
| Qwen2.5-7B | AxBench Prompt | 50.00% | 50.00% |
| Gemma 3 12B | Unsteered | 1.25% | 1.25% |
| Gemma 3 12B | Calibrated CAA | 1.25% | 1.25% |
| Gemma 3 12B | DiffMean | 32.50% | 52.50% |
| Gemma 3 12B | NLA steering | 13.75% | 37.50% |
| Gemma 3 12B | AxBench Prompt | 55.00% | 55.00% |

## Paired method contrasts after word tuning

| Model | Comparison | Difference | Descriptive 95% CI |
|---|---|---:|---:|
| Qwen2.5-7B | NLA minus DiffMean | +3.75 points | [-27.50, 31.25] |
| Qwen2.5-7B | NLA minus calibrated CAA | +46.25 points | [22.50, 71.25] |
| Gemma 3 12B | NLA minus DiffMean | -15.00 points | [-48.75, 18.75] |
| Gemma 3 12B | NLA minus calibrated CAA | +36.25 points | [16.25, 57.50] |

The point ordering changes narrowly to NLA on Qwen and remains in favor of
DiffMean on Gemma. Both NLA-minus-DiffMean intervals cross zero. The result
therefore shows sensitivity to the tuning objective and no reliable winner.

## Administration

- Ten concepts, four held-out prompts per concept, and two paired generation
  seeds give 80 held-out rows per named arm and model.
- Four binary calibration rows select alpha for each method and concept. The
  smallest alpha wins every tie.
- Alpha changes for 18 of 30 method-concept pairs on Qwen and 20 of 30 on
  Gemma.
- Eight Qwen and seven Gemma method-concept grids have no calibration hit at
  any tested alpha. These select the grid minimum under the fixed tie rule.
- Each treatment is compared separately with matched random seeds 17, 42, and
  101. No strongest random was selected.
- The Qwen union contains 1,096 rows and the Gemma union contains 1,024 rows.

## Provenance

| Artifact | SHA-256 |
|---|---|
| Strict analysis | `deb66099635f9e9c164b784cde0e6f293d0e84e7f9e49e31d3f8ecf360561bac` |
| Qwen archive | `63730759e3ccf625c0942fac841834d68c981e667215364061f8391256e59544` |
| Gemma archive | `e6e104cfb0034c853781fe6ca40abb2387398caa56f6dbddc9abe1a9888e1c0d` |
| Qwen sensitivity COMPLETE | `2ff69e4031262d1aa23561021d428f1fba540ed06bce5a0b3f52081d07c43298` |
| Gemma sensitivity COMPLETE | `4d4a2fdbb48d7f6a819ac45ceaf4c8b94b9a62de26348c59f67758141c015096` |

Analysis code commit: `19eaacf`. RunPod pod `3i08n8e4yv926z` was confirmed
`EXITED` after both archives were retrieved and verified.

## Interpretation boundary

This analysis is post-hoc and descriptive. The original H-tuned experiment
remains primary. Matching the tuning objective removes one difference between
the administrations, but the concept panels still differ in their concepts,
prompts, and word lists.
