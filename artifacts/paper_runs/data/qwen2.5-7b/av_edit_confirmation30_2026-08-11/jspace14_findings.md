# J-space analysis of 14 automated AV-edit passes

This selected-set analysis uses the existing Qwen layer-20 Jacobian lens,
fitted on 12 prompts. It covers 112 prompt-specific vectors and 14 centroids.

## Findings

- Within-concept cosine to the centroid ranges from 0.716 to 0.807.
- Rank-32 J-space projection fractions range from 0.211 to 0.325.
- Several distinct concepts have highly similar centroids: c287/c90 reaches
  0.866, c354/c171 reaches 0.834, and c11/c186 exceeds 0.812.
- At alpha 0.5, transported-versus-actual one-step logit shifts have mean
  cosine 0.257 and mean top-50 overlap 2.95 tokens.
- At alpha 0.01, mean cosine is 0.044 and mean top-50 overlap is 0.60, so
  excessive alpha alone does not explain the weak agreement.

The geometry indicates that append edits induce stable but often broad,
shared semantic directions. This helps explain lexical false positives based
on generic terms such as "context", "specific", and "used".

c186 is a relatively faithful case: JLens predicts military/tactical tokens,
and the actual shift ranks "military" second. In contrast, c406 predicts
programming tokens while the best actual target term ranks 794; c354 predicts
"software" first while its actual rank is 845.

## Conclusion and limitation

Append-style AV edits create repeatable activation directions and can cause
steering, but many directions are generic or semantically entangled rather
than concept-specific. The analysis does not independently validate all 14
automated passes.

The 12-prompt lens has weak transported-logit agreement. A stronger token-level
causal claim requires refitting a Qwen layer-20 lens on roughly 100--1000
representative sequences and repeating the held-out readout.
