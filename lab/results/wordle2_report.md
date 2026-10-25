# Wordle 2 results: tested length directions remain null; dwelling shifts choices

> **Current audited status (2026-08-19).** Under the
> [current taxonomy](../docs/METHOD_TAXONOMY.md), all swept dwelling,
> lion, and length parents in this campaign are authored AR text contrasts
> (`nla_ar_contrast`), not canonical AV-edit-AR. The planned AV-derived
> one-word number swaps and five-letter anchor did not pass build gates and
> have no behavioral sweep here. The dwelling split is
> `nla_ar_contrast / jspace_sparse_nonnegative / component|remainder`: a
> reference-inspired full-vocabulary greedy-NNLS k25 J-atom approximation
> and parent-minus-approximation remainder, each independently unit-normalized.
> All behavioral sweeps inject at Qwen layer 20 on every prompt and decode
> position (`prompt_and_decode_all`) with relative-token-L2 scaling.
> Current conclusion: the tested length constructions did not beat random or
> degradation, while the whole dwelling parent increased HOME on two of four
> prompts. This does not establish a general "meaning beats form" law or an
> undefeated J-lens forecast rule. See the

Technical report, 2026-08-15 (overnight run). Pre-registration:
`experiments/wordle2/README.md` (committed before results, including
all decisions delegated during the designer's absence). Raw
generations: `results/wordle2-*.json` (10 steering sweeps + census, 3,060
replies). Vectors, screens, and both build rounds:
`experiments/wordle2/data/`. Figures: `analysis/figures/wordle2/`.

## Census under wordle-only prompts

The designer's prompt rider (say "wordle", never "five letter word")
produces softer walls than the formatted prompt used in August: CRANE
48 percent (29/60) and only 62 percent of answers are five letters
(census range 3 to 8). All effects below are read against this softer
baseline and against the random control at matched strengths.

## Screening gates did most of the work (two rounds, all archived)

The verbalizer's description of the wordle state contains no "five",
so every one-word number-swap arm self-gated out. Round-A freeform
length pairs read out as byte junk; round-B enrichment recovered
LONGER-ness content (readouts: extra, additional, more, longer,
bigger) but number identity never entered any vector, the lion
meaning never built (readout: quote-marks and "supreme"), and
"exactly five letters" never built (readout: pure punctuation). The
composition arm was gated out by its own precondition. The best-length
selection rule honestly returned none (no candidate carried weight on
its own number word).

## Result 1: the tested AR-contrast length directions do not steer length

All four enriched length arms and the junk-readout control track the
random control within noise: every arm (including random) drifts
upward by ~0.5 letters at mid strengths, which raw-text audit
attributes to degradation pushing the extractor onto ordinary prose
words. No arm moves toward ITS OWN target (the aim-four arm does not
shorten relative to random; no arm exceeds random's drift). Clean,
matched-control negative: with reconstructor-built vectors at layer
20, these authored AR text contrasts did not make formal count a
steering lever, consistent with the lens screens (the number never
survived into any vector) and with the
matched random/degradation pattern. This is evidence against the tested
constructions, not proof that answer length is unsteerable. See
length_null.png.

## Result 2: the whole dwelling AR contrast shifts answers toward HOME

The dwelling vector (verbatim August sentences) on wordle-only
prompts, dose-resolved (dwelling_outcomes.png): CRANE 29/60 at
baseline, 4/60 at strength 0.2, zero from 0.4 up. HOME becomes the
modal answer at 0.4 and 0.6 (12/60 and 11/60; random control: zero
HOME ever, and CRANE survives at 12.5 percent under random at matched
strengths, so the wall-break plus the specific landing is the
vector's content, not the shove). Five-letter dwelling forms remain
almost absent (HOUSE 0 and 1). The effect appears on two of four prompts;
across the two headline doses, the whole parent yields HOME in 23/120
replies. Because the unsteered census produces a valid five-letter answer
only 62 percent of the time, this campaign does not provide a hard format
constraint whose universal loss can be inferred. It supports transfer of
the dwelling association beyond the original pointed prompt, with limited
prompt-level generality.

## Result 3: neither isolated k25 piece reproduces the whole dwelling effect

Unlike several emoji arms (where the unit-normalized k25 component
steered at lower dose), the dwelling parent's greedy-NNLS k25 component
produces generic wordle-talk (top answers: word, hello, start; zero HOME)
despite its selected atoms being houses/homes tokens, and the k25
remainder produces HOME only 3/120. The whole vector's 23/120 HOME is not localized in
either equal-norm piece under this protocol. Selected token-direction
content was therefore not sufficient to reproduce this parent's
discrete-choice effect. Possible explanations include distributed
composition, solver approximation, independent normalization, and
context/layer mismatch; the result does not identify which mechanism is
responsible.

## Result 4: two negative arms were consistent with their readouts

The deliberately-swept junk-readout arm behaved exactly like random.
The lion arm, whose readout predicted royal-register rather than
cat-content, produced zero LION and zero TIGER in this Qwen campaign.
These are useful consistencies, but two non-blinded cases do not establish
a forecast rule. Target-aligned readout remains a candidate diagnostic;
scalar J-gain and sparse share do not reliably rank success in the wider
ledger.

## Open questions this hands the team

1. Why does this dwelling choice fail to localize when several style
   effects concentrate per unit norm? Native-magnitude pieces and solver
   variants can separate decomposition from normalization effects.
2. Can a construction preserve a strong five-letter constraint while
   steering content? A matched CAA arm and a prompt with verified format
   compliance are needed; no archived CAA result exists yet.
3. Use readouts to triage expensive sweeps, while retaining random controls
   and prospective behavioral tests rather than treating readout as a hard
   pass/fail gate.

Cost: build rounds + 10 steering sweeps and one census ~ $4. Session
total (wave 2 + wordle) ~ $16 GPU across 7.3 pod hours.
