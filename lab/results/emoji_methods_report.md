# Emoji steering by construction method, with a J-space decomposition test

> **Current audited status (2026-08-19).** This report contains a historical
> wave-1 analysis followed by the larger wave-2 correction. Under the
> [current taxonomy](../docs/METHOD_TAXONOMY.md), the freeform parent is an
> authored AR text contrast (`nla_ar_contrast`), whereas the substrate edits
> are canonical AV-edit-AR directions (`nla_av_edit_ar`). The wave-1 top-50
> signed OLS split is the legacy `j_readout_ols_split` and is quarantined from
> current decomposition claims. Wave 2 uses a reference-inspired,
> full-vocabulary greedy-NNLS k25 J-atom approximation
> (`jspace_sparse_nonnegative`) and its parent-minus-approximation remainder;
> it is not the paper's unpublished exact solver. Every piece was independently
> unit-normalized, so component/remainder comparisons measure potency per unit
> norm, not native causal contribution. Current conclusion: target-aligned
> readout is a promising diagnostic, not an established or undefeated
> forecast, and k25 remainders are not universally inert. See the

Technical report, 2026-08-14. Experiment designed via the team's
worksheet process; all predictions pre-registered in
`experiments/emoji_methods/README.md` (committed before any run).
Raw generations in `results/emojimx-*.json`, vectors and screening in
`experiments/emoji_methods/data/`, figures and per-arm summary in
`analysis/figures/emoji_methods/`.

## Abstract

We compare emoji steering from an authored AR text contrast and from
canonical AV-edit-AR directions at single-site and phrase edit sizes.
Wave 1 also intervened on a legacy top-50 signed-OLS component and
remainder of the freeform parent. That split concentrated the effect in
the unit-normalized component for this one parent, but it is a historical
proxy rather than the current sparse decomposition and does not establish
a universal active ingredient. Wave 2 corrected the target-word controls
and used full-vocabulary greedy-NNLS k25 approximations. All corrected
single-site edits and phrase edits steered emoji use, and k25 components
often activated at lower normalized dose; several k25 remainders also
steered near parent strength. Readout content tracked several observed
behaviors, while scalar transport gain, sparse share, and reconstruction
amplitude did not rank success. Because readouts were inspected on a small,
non-blinded set, they remain a candidate pre-flight diagnostic requiring
prospective validation.

## Terminology

- Authored AR text contrast (`nla_ar_contrast`): the freeform parent is
  AR(authored positive description) minus AR(authored negative
  description). It does not use an activation's exact AV text and is not
  canonical AV-edit-AR.
- Canonical AV-edit-AR (`nla_av_edit_ar`): the single-site and phrase arms
  edit the AV description of an actual layer-20 substrate state, then use
  AR(edited AV text) minus AR(original AV text).
- Steering: activation addition during generation at every position,
  h <- h + alpha * ||h|| * d, layer 20 of Qwen2.5-7B-Instruct
  (`prompt_and_decode_all`, relative-token-L2 scaling).
- Jacobian lens: the averaged Jacobian J_20 = E[dh_final / dh_20]
  fitted with Anthropic's reference implementation
  (anthropics/jacobian-lens) over 160 WikiText-103 passages
  (split-half reliability 0.943, gate 0.90; see
  `experiments/jspace/`). Readout of d: the top of the token score
  vector (gamma element-wise W_U) J_20 d. Transport gain:
  ||J_20 d||, with E||J_20 x|| = 1.244 for arbitrary unit x and
  1.21 to 1.23 measured for seeded random unit vectors.
- Legacy wave-1 split (`j_readout_ols_split`): signed ordinary least
  squares over the freeform parent's own top-50 readout atoms. Its
  component is an orthogonal projection onto that selected span, and its
  remainder is parent minus projection. This is not sparse nonnegative
  coding and is quarantined from wave-2/current claims.
- Wave-2 sparse split (`jspace_sparse_nonnegative`): a full-vocabulary
  greedy maximum-positive-correlation selection with exact NNLS refits;
  headline role is the k25 J-atom approximation (`component`) or parent
  minus that approximation (`remainder`). This is reference-inspired and
  diverges from the paper's unpublished gradient-pursuit solver. It does
  not establish a globally "verbalizable" versus "non-J" partition.
- Vector view: parents, components, and remainders are each normalized to
  unit L2 before steering. Dose comparisons therefore concern equal-norm
  potency, not the pieces' native contribution to the parent.

## Wave-1 method (historical)

14 arms, identical protocol per arm: 5 neutral prompts x 5 strengths
(alpha in {0, 0.2, 0.4, 0.6, 0.8}) x 15 samples (seeds reused across
alphas, paired design), temperature 0.7, 60 max new tokens. 5,250
generations total. Target probe: emoji character count (deterministic).
Secondary: exclamation count. Coherence gate: an alpha is admissible
only if mean unique_word_ratio >= 0.8x its own alpha-0 mean AND mean
word count >= 0.5x baseline; the word-count clause was added after the
run when raw-text audit showed pure-emoji degeneration scoring 0.99 on
unique_word_ratio (the probe's documented unspaced-stream blind spot).
Effect size: Cliff's delta versus the same arm's alpha-0 sample,
reported at the admissible alpha with the largest absolute value.
Substrate for verbalize-edit arms: the verbalizer's greedy description
of the layer-20 state of the first test prompt; single-token edits
insert one adjective at a fixed site, phrase edits alter one clause
each (all ten edits verbatim in `experiments/emoji_methods/data/
edits.json` and `build_report.json`, with delta ratios, echoes, and
readouts recorded before any steering). Steered vectors are loaded
from the screened npz byte-identically (runner `direction_file`
support, added for this experiment).

## Wave-1 per-arm results

| arm | construction | delta ratio | gain | readout (top) | best delta | emojis @0.4 / @0.6 |
|---|---|---|---|---|---|---|
| freeform | `nla_ar_contrast`: 2 authored sentences | - | 2.00 | ! ! [emoji byte] crazy | +0.88 @0.8 | 0.08 / 2.96 |
| single_1 | `nla_av_edit_ar`: +"excited" | 0.026 | 1.99 | exciting excitement excited ! | +0.01 | 0.00 / 0.03 |
| single_2 | `nla_av_edit_ar`: +"enthusiastic" | 0.028 | 2.15 | exciting excitement ! amazing | +0.05 | 0.00 / 0.11 |
| single_3 | `nla_av_edit_ar`: +"cheerful" | 0.016 | 1.80 | happy happy nice cheerful | +0.09 | 0.00 / 0.05 |
| single_4 | `nla_av_edit_ar`: +"expressive" | 0.018 | 1.71 | laughter fancy funky quirky | +0.17 | 0.00 / 0.45 |
| single_5 | `nla_av_edit_ar`: +"emoji" | 0.020 | 2.10 | [emoji byte] ... | +0.33 @0.4 | 1.63 / 36.1 (degen.) |
| phrase_1 | `nla_av_edit_ar`: packed with emojis and exclamation marks | 0.060 | 1.90 | hilarious crazy ?! | +0.88 @0.6 | 1.55 / 6.87 |
| phrase_2 | `nla_av_edit_ar`: emoji-filled, excitable continuation | 0.040 | 1.89 | )! excitement Wow | +0.71 @0.6 | 0.27 / 2.49 |
| phrase_3 | `nla_av_edit_ar`: emoji-heavy greeting, playful energy | 0.077 | 1.94 | fancy silly funny | +0.68 @0.6 | 0.56 / 2.05 |
| phrase_4 | `nla_av_edit_ar`: literal glyphs inserted | 0.039 | 1.87 | [emoji bytes] x5 | +1.00 @0.6 | 2.13 / 19.3 |
| phrase_5 | `nla_av_edit_ar`: overflowing with emojis, exclamation marks | 0.079 | 1.91 | sexy ! ?! hilarious | +0.80 @0.6 | 0.67 / 2.71 |
| legacy OLS component | `nla_ar_contrast / j_readout_ols_split / component`, unit L2 | - | 5.01 | ! ! [emoji byte] crazy | +0.68 @0.4 | 5.64 / 16.1 (degen.) |
| legacy OLS remainder | `nla_ar_contrast / j_readout_ols_split / remainder`, unit L2 | - | 2.09 | token noise | +0.31 @0.8 | 0.00 / 0.12 |
| random | `random_isotropic`, unit L2 | - | 1.21 | token noise | 0.00 | 0.00 / 0.00 |

Figures: `../analysis/figures/emoji_methods/split_verdict.png`
(decomposition arms), `methods.png` (by construction method),
`forecast.png` (historical readout/behavior comparison),
`ranking.png` (gain vs effect), `split_anatomy.png` (the
decomposition itself).

## Wave-1 finding 1: the legacy OLS component concentrated this effect

At alpha 0.4 the full freeform vector produces 0.08 emojis per reply;
its legacy OLS component alone, renormalized, produces 5.64 in
coherent text (delta 0.68). By alpha 0.6 the component overdoses into
emoji-dominated degeneration. The legacy OLS remainder produces 0.00 at
alpha 0.4 and 0.12 at 0.6; its weak 0.8 effect (0.87 mean, delta 0.31)
is consistent with imperfect separation by the top-50 span (content
outside the selected span can remain in the remainder). The random control is
flat at every alpha. For this parent and this historical split, equal-norm
steering potency was concentrated in the selected top-50 span. This does
not show that the span contains the entire native causal effect, because
both pieces were renormalized, nor that remainders from other parents or
the later k25 split are inert. The component's gain was 5.01 versus 2.00
for the parent, but later arms show that gain alone does not rank success.

## Wave-1 finding 2: edit size was confounded with target wording

Phrase-level canonical AV-edit-AR arms steered as strongly as the
authored AR-contrast parent in wave 1. Four of five single-site edits,
however, named excitement rather than emoji use, so wave 1 did not isolate
edit size. The corrected target-naming single-site arms in wave 2 all
steered emoji use. The supported conclusion is that construction wording
and target alignment mattered in this set; neither a universal edit-size
law nor single-token bottleneck death follows.

## Wave-1 finding 3: readout content was behaviorally suggestive

Several readout/behavior correspondences motivated the diagnostic
hypothesis. The two arms whose readouts contained exclamation tokens
raised exclamations from 0.83 to 4.2 and 4.8 per reply at alpha 0.6
(random control declined to 0.55);
the "cheerful" arm (happy-word readout) rose to 3.2; the "expressive"
arm, whose readout contained no exclamation token, stayed at baseline.
The word-identity arm ("emoji") and the glyph arm produced emoji
characters directly, flooding at high alpha. These observations argue
against the former 0.15 delta-norm-ratio gate (phrase_4 had ratio 0.039
and delta 1.00) and motivate target-aligned readout as a candidate
pre-flight diagnostic. They do not establish a forecast rule: the set was
small, mappings from tokens to behavioral probes were not evaluated
blindly, and scalar gain/share failed to rank outcomes.

## The legacy wave-1 split is a projection, not coordinate deletion

A natural misreading of "split the vector" is that coordinates were
zeroed. They were not: both pieces are dense (0 of 3,584 coordinates
are exactly zero in either), every original coordinate is the exact
sum of its two pieces (max reconstruction error 2.4e-8), and the
wave-1 pieces are exactly orthogonal. The selected span is not
axis-aligned, so
in coordinate view both pieces look like full vectors; sparsity exists
only in the 50 selected coefficients used to construct the OLS component.
The remainder is orthogonal to that selected span, but may still align
with unselected J-lens atoms; it is not a globally non-J direction.
Example coordinates:

| coordinate | original | = component part | + remainder part |
|---|---|---|---|
| 1 | -0.00171 | -0.00083 | -0.00088 |
| 2 | +0.00508 | -0.00163 | +0.00671 |
| 3 | +0.00419 | +0.00102 | +0.00317 |

See `split_anatomy.png`.

## Limitations

One model (Qwen2.5-7B-Instruct), one layer (20), one behavior family,
one substrate paragraph for all AV-edit-AR directions, n = 15 per cell,
5 prompts. The historical wave-1 component uses a top-50 own-readout
span, not the wave-2 sparse nonnegative approximation; the remainder's
weak high-alpha effect suggests the selected span under-captures.
Single-site edit locations were all in one clause.
Coherence gating partially relies on a post-hoc (though deterministic
and disclosed) word-count rule. Cliff's delta at n = 15 per cell has
wide uncertainty for mid-range values; the headline contrasts
(0.68 vs 0.00 at matched alpha; 0.88 to 1.00 vs 0.01 to 0.17) do not
depend on precision. The single-vs-phrase comparison on the emoji
probe partly confounds edit size with edit content: four of five
single-token insertions are excitement words that do not mention
emojis, while all five phrase edits do (spotted in review by the
experiment's designer). The limited within-content comparison---one
"emoji" single versus two emoji-naming phrases---suggests different dose
windows but does not by itself isolate edit size. On the exclamation
probe, the excitement singles change their content-matched behavior, so
their low emoji score should not be interpreted as general vector
inactivity.

## Wave 2 (2026-08-14 evening): corrected singles and sparse NNLS approximation

Design review by the experiment's designer produced three changes, all
run the same day: five target-relevant single-token edits replacing
the excitement-word set for the size question; a reference-inspired
J-atom approximation (full-vocabulary greedy positive-correlation
selection with exact NNLS refits, k in {10, 16, 25}; not the paper's
unpublished exact solver; the v1 top-50 signed projection is superseded
and its numbers quarantined); fresh-seed
reruns of every wave-1 arm (seeds 15-29), pooling to n = 30 per cell.
44 sweeps, ~6.5 h, results in results/emojimx2-*.json, figures in
analysis/figures/emoji_methods_v2/.

1. Reproducibility: the rebuilt "emoji" single's sweep results are
   digit-identical to wave 1's (same seeds, deterministic pipeline,
   different pod session). Pooled reruns confirm every wave-1
   estimate (freeform 0.84 vs 0.88; phrase_4 1.00; random 0.00).
2. Corrected singles: ALL FIVE target-relevant one-word edits steer
   (delta 0.32 to 1.00; the bare glyph reaches 1.00, matching the
   best phrase edit), where the excitement singles managed 0.04 to
   0.16 on the emoji probe. Wave 1's "single-token edits fail" is
   therefore refined: one token steers its own content; the wave-1
   singles failed the emoji probe because they named excitement, not
   emojis. Token-level vectors remain flood-prone at high alpha.
3. k25 component-norm ratios are 0.17 to 0.28 across all 16 content
   vectors, and the ratio does not rank emoji steering success
   (excitement-word singles carry ratios as high as target-matched
   winners). Target alignment is the more useful pattern in this set,
   but it has not yet been validated as a blinded predictor.
4. Equal-norm potency concentration appears under the k25 approximation
   and across root constructions: the k25 component fires at
   roughly half the parent's dose (freeform component onset 0.4 vs
   0.6; glyph-phrase component onset 0.2 vs 0.4; glyph-single
   component 0.63 delta at 0.4), then overdoses into floods.
5. The important complication: under the k25 approximation the
   remainders are not inert. Phrase and new-single remainders steer at
   near-parent strength (0.61 to 0.97); only the freeform remainder is
   weak (0.36). The v1 projection (50 tokens, signed least squares,
   24 percent of length) produced a near-inert remainder for one parent;
   the 25-atom nonnegative approximation (about 20 percent of length)
   does not do so across parents. The selected atoms capture a
   high-potency core per unit norm, not a complete active ingredient.
   Because each piece is independently normalized, these arms do not
   measure how much native parent effect each piece contributes.

## Implications and queued follow-ups

1. Compare solver variants and k values using both native-magnitude and
   unit-normalized pieces on held-out behavior families.
2. Use target-aligned readout as a candidate screen, but validate frozen
   predictions prospectively; do not use scalar gain/share as a success
   criterion.
3. Fix unique_word_ratio for unspaced streams in probes.py (the
   word-count guard currently lives only in this experiment's
   analysis).
4. Direct J-lens-atom intervention (with no AR parent) is the natural
   head-to-head; no archived result currently performs that experiment.
