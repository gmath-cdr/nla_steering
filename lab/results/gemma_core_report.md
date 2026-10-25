# Gemma-3-12B cross-model replication: stronger lower-dose steering

> **J-space correction notice (2026-08-21).** The historical Gemma atom
> dictionary multiplied by raw RMSNorm `weight`; Gemma applies `1 + weight`.
> The merged layer-32 lens, whole-parent vectors, and their behavioral runs
> remain valid. Token readouts and all greedy-NNLS k25 component/remainder
> constructions are under audit. Their archived outputs remain measurements of
> the exact saved vectors, but those vectors are not the intended actual-logit
> decomposition. Treat predictions/findings 3, 4, 6 and J-readout portions of
> the later discussion as suspended until corrected rebuilds and selected
> reruns are complete.

> **Current audited status (2026-08-19).** Under the
> [current taxonomy](../docs/METHOD_TAXONOMY.md), the freeform emoji,
> dwelling, lion, and length parents are authored AR text contrasts
> (`nla_ar_contrast`), not canonical AV-edit-AR. The five substrate
> single-site edits and two phrase edits are canonical AV-edit-AR
> (`nla_av_edit_ar`). Their derived arms use the reference-inspired
> full-vocabulary greedy-NNLS k25 J-atom approximation
> (`jspace_sparse_nonnegative`) or its parent-minus-approximation
> remainder; each is independently unit-normalized. This is not the
> paper's unpublished exact solver, and the remainders are not universally
> inert. All behavioral sweeps inject at Gemma layer 32 on every prompt and
> decode position (`prompt_and_decode_all`) with relative-token-L2 scaling.
> The Qwen/Gemma comparison is consistent with a capacity or
> representation hypothesis but confounds model family, architecture,
> training, tokenizer, layer, and size, so it is not a clean capacity
> result. The raw lion run includes TIGER, and target-aligned readout is a
> promising diagnostic rather than an established forecast. See the
> [current research state](../codex_context/CURRENT_STATE.md) and
> [evidence ledger](../codex_context/EXPERIMENT_LEDGER.md#9-gemma-3-12b-core).

Technical report, 2026-08-15 overnight. Pre-registration:
`experiments/gemma_core/README.md` (seven predictions, committed
before any run). 19 steering sweeps + census, 6,735 replies, Gemma alpha ladder
0/.05/.1/.15/.2 (its measured window). Raw: `results/gcore-*.json`.
Lens: layer 32, identical 160-passage corpus, split-half 0.976.
Figures: `analysis/figures/gemma_core/`.

## Scorecard against the pre-registered predictions

1. Freeform steers with a coherent window; random is target-flat but
   destructive: HELD WITH QUALIFICATION. Emojis
   0 -> 3.0/reply at alpha 0.1 in coherent text; flood 0.15, collapse
   0.2. Random: zero emojis at every strength, although it is not
   behaviorally flat because mean reply length collapses to 5.1 words at
   alpha 0.1.
2. Corrected singles steer; glyph near top: HELD as a family (all
   five steer; 4.4 to 17.7 mean emojis at 0.15). Rank order shuffles
   across models: "smiley" is Gemma's strongest single and was Qwen's
   weakest; the family-level effect replicates across these two
   checkpoints, while the within-family ranking does not.
3. Readout-as-forecast: CAMPAIGN-CONSISTENT, NOT ESTABLISHED.
   Emoji-content readouts accompany emoji behavior and the junk-readout
   length parent is null (see 7). This small, partially theory-informed
   set is not a blinded prospective validation, and scalar gain/share do
   not rank success in the wider archive.
4. Component concentration and remainder activity: MIXED. The
   unit-normalized freeform k25 component fires at alpha 0.05, a quarter
   of the parent's onset dose, with 26 emojis/reply where the parent is
   still at zero. Its k25 remainder stays at or below 0.19 emoji/reply.
   That quietness is parent-specific: the glyph-phrase k25 remainder
   reaches 0.97 emoji/reply at alpha 0.15 (19/75 affected, mean 23.6
   words) and floods to 6.47 at 0.2; the dwelling remainder also has a
   substantial late effect (see 6). The supported conclusion is
   equal-norm potency concentration for selected parents, not universal
   localization or inert remainders.
5. Dwelling: wall breaks and target-family meaning lands: HELD as a
   directional result; the capacity interpretation remains unresolved.
   Gemma's census wall (CRANE 35 percent, ADIEU second) dies by 0.1;
   at 0.15 HOUSE is modal (28 percent); at 0.2 HOUSE reaches 62
   percent with HOME at 15. Qwen's tested dwelling parent favored HOME,
   whereas Gemma's favored HOUSE. This is consistent with the recorded
   larger-model hypothesis, but the two checkpoints differ on many axes
   besides parameter count and do not confirm a capacity threshold. See
   dwell_crossmodel.png.
6. Dwelling decomposition: MIXED. The unit-normalized greedy-NNLS k25
   component's modal answers at 0.15-0.2 include "casas" (Spanish) and
   "rumah" (Indonesian)---houses in other tongues---often in degrading
   text. The whole vector
   lands English HOUSE. The k25 remainder is not merely a weak trace: at
   alpha 0.2 it yields HOUSE 17/60 plus HOME 3/60, compared with the
   whole parent's HOUSE 37/60 plus HOME 9/60. The multilingual component
   transcripts are suggestive of semantic content without stable English
   binding, but they do not establish that "purification" removes language.
7. Length behaves like random: HELD. Mean answer letters flat at
   ~4.8 at every strength. The tested authored length contrasts are null
   on both checkpoints; length steerability in general remains open.

## Findings beyond the predictions

- The lion concept ENCODES on Gemma (readout: lions tigers leopards
  king) where Qwen's reconstructor failed in two authoring rounds;
  steering it drives the answer to ROAR (modal at 0.15, 52 percent)
  and KING. Contrary to the original report, leading TIGER occurs in
  5/60 replies at alpha 0.15 and 4/60 at 0.2; LION itself does not lead.
  Concept readout and landing behavior differ across these checkpoints,
  but TIGER prevents the stronger claim that format never bent meaning.
- The content-versus-random degradation asymmetry replicates: Gemma's random
  control destroys text (1-5 words per reply at 0.1+) while content
  vectors at the same strengths still write sentences.
- Reconstructor amplitude compresses on Gemma: phrase-edit delta
  ratios 0.007-0.015 (vs Qwen's 0.04-0.08) with intact content and
  observed steering. The old amplitude-based screening would have
  discarded every working Gemma vector.

## Costs and provenance

Pod session ~8 h ~ $12.7 (including a ~40 min license-gate wait).
Port validated end to end: the config-table refactor, the multimodal
layout resolvers, the tied-embedding lens machinery, and the fix for
the embedding double-scaling bug (invisible on Qwen for a month) are
all in the lab layer now. Substrate, edits, echoes, readouts, shares,
and both split-half lens halves archived under
`experiments/gemma_core/data/` and `experiments/jspace/data/`.
