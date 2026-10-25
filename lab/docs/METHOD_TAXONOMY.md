# Steering method taxonomy

Status: authoritative vocabulary
Schema: `steering-taxonomy/v1`
Adopted: 2026-08-19

## Why this exists

“NLA steering” is a useful project-level umbrella, but it is not a sufficient experimental label. The lab has used several distinct root constructions, then transformed some parents into components/remainders, normalized them, and administered them under different hook schedules. Those choices answer different scientific questions.

Every arm must therefore be represented as this chain:

```text
residual space
  → root vector construction
  → optional derivation from a named parent
  → vector view (native or normalized)
  → intervention protocol
  → study/evidence record
```

J-lens readout is a diagnostic. Sparse decomposition is a derivation. Neither is interchangeable with a root construction.

## 1. Residual space

A vector only has meaning in a specific activation space. The minimum identity is:

- base model/checkpoint;
- residual site and layer indexing convention;
- hidden width;
- tokenizer/model family where relevant.

Current registered spaces are in `lab/registry/residual_spaces.yaml`.
Saved vector archives and J-lens files are byte-identified in
`lab/registry/artifacts.yaml`; the experiment manifest additionally hashes the
exact NPZ member used by every saved-vector run.

Invariant: a parent vector, J-lens dictionary, and intervention target must share a `space_id` unless an explicit transport map is registered.

## 2. Root constructions

### `nla_av_edit_ar`

An activation is actually extracted and verbalized, that exact AV text is edited, and the edited/original reconstructions are subtracted:

```text
h → S = AV(h)
S → S_edit
d_raw = AR(S_edit) - AR(S)
```

Required provenance:

- substrate prompt/state selector and activation space;
- AV and AR checkpoint identities;
- exact original AV text and exact edited text;
- structured edit and granularity;
- raw difference norm and vector artifact/hash.

A researcher-written sentence that merely resembles an AV explanation is not `nla_av_edit_ar`.

### `nla_ar_contrast`

Two authored/template/data descriptions are passed directly to AR:

```text
d_raw = AR(T_positive) - AR(T_negative)
```

Required provenance:

- exact positive/negative texts;
- text origin (`authored`, `template_instantiated`, or `dataset`);
- pairing style (`matched_scaffold` or `unconstrained`);
- AR checkpoint identity;
- raw difference norm and vector artifact/hash.

Running the result through AV as an echo is a diagnostic; it does not turn this into AV-edit-AR.

### `caa_mean_activation_contrast`

```text
d_raw = mean_i(h_positive_i - h_negative_i)
```

Required provenance includes paired dataset/hash, pair IDs/count, activation aggregation rule, layer/site, orientation, and mean rule.

No archived lab result currently uses CAA.

### `j_lens_atom`

A single registered atom from a fitted J-lens dictionary. Required provenance includes the lens/dictionary ID, fitting-corpus hash, token ID/rendering, formula convention, and atom normalization.

No archived lab result currently injects a pure J-lens atom.

### `random_isotropic`

An isotropic random control, currently a standard-Gaussian vector normalized to unit L2. Record distribution, dimension, vector RNG seed, generator/version, and hash. Keep the vector seed distinct from generation sample seeds.

### `none`

Used only for an unsteered baseline/census. This is the absence of a vector, not a vector-construction technique.

## 3. Text provenance axes

These fields refine the root construction; they do not replace it.

| Field | Controlled values currently used |
|---|---|
| `text_origin` | `authored`, `template_instantiated`, `dataset`, `av_generated_substrate`, `not_applicable` |
| `contrast_relation` | `paired_descriptions`, `edited_vs_original`, `matched_activation_pairs`, `not_applicable` |
| `edit_granularity` | `single_site`, `phrase`, `clause`, `document`, `not_applicable` |
| `pairing_style` | `matched_scaffold`, `unconstrained`, `not_applicable` |

“Single-site” is deliberately not a root method. A one-word difference can occur in an AV-derived edit or in a hand-authored AR pair.

## 4. Derived vectors

A derived vector retains the exact root ancestry of its parent.

### `jspace_sparse_nonnegative`

The current corrected implementation is:

- full-vocabulary J-lens atom candidate pool;
- unit-L2 atoms;
- greedy maximum positive-correlation selection;
- exact nonnegative least-squares refit at every step;
- requested `k=25` for headline arms.

It produces two roles:

- `component`: the greedy-NNLS k25 approximation;
- `remainder`: parent minus that approximation.

Required label example:

```text
nla_ar_contrast / jspace_sparse_nonnegative / component / unit_l2
```

Never shorten this to “J component” when ancestry matters.

The implementation is reference-inspired and diverges from the paper's unpublished gradient-pursuit solver. The remainder is not established to be orthogonal, globally non-J, dead, null, or unverbalizable. It may align with unselected atoms and may route back through J coordinates downstream.

Preserve both native component/remainder and any unit-normalized views. Native views answer contribution questions; unit views answer potency-per-norm questions.

### `j_readout_ols_split`

Legacy emoji v1 only: signed ordinary least squares over the freeform parent's top-50 readout atoms. Its roles are `component` and `remainder`.

This is not the nonnegative sparse J-space decomposition and must never be pooled with the k25 results.

### `linear_combination`

A weighted mixture of named parents. Record every parent ID, coefficient, pre/post-normalization rule, and orientation.

## 5. Vector view

The vector view is separate from its ancestry:

- `native`: the magnitude produced by the construction/derivation;
- `unit_l2`: direction independently normalized to L2 norm 1;
- `none`: unsteered baseline.

All standard archived runner interventions use `unit_l2`. Therefore component/remainder comparisons are currently equal-norm potency comparisons, not native causal decompositions.

## 6. Intervention protocol

Current runner intervention:

```text
h_prime = h + alpha * ||h||_2 * unit_l2(d)
```

Controlled token scopes:

| Runner mode | Taxonomy value | Meaning |
|---|---|---|
| `all` | `prompt_and_decode_all` | Every prompt-prefill position and each decoded position |
| `decode` | `decode_only` | Generated positions only |
| `position` | `single_prompt_position` | One selected prompt position during prefill |

Token scope is not vector construction. In particular, `mode: all` does not make a vector CAA.

## 7. Study/evidence record

Evidence intent lives on a run/claim, not the vector. The same frozen vector can first be exploratory and later be used in a confirmation.

Controlled experiment intent:

- `screening`
- `exploratory`
- `confirmatory`
- `unknown` for legacy work without a defensible locked record

Controlled arm role:

- `baseline_reference`
- `negative_control`
- `treatment`

An alpha-zero cell inside a steering sweep is a within-sweep zero dose; a separate no-vector run is an unsteered census. These should not be conflated.

Confirmatory specs must carry explicit method metadata. A confirmatory claim additionally requires a frozen vector/hash, model/space, prompts/data, sample size/seeds, alpha rule, endpoint/extractor, exclusions/coherence rules, analysis, and stopping rule.

## 8. Current audited corpus

The generated manifest classifies all 137 standard runs and 37,271 replies with zero unresolved method records:

| Root construction | Whole | k25 component | k25 remainder | Legacy OLS component/remainder | Runs | Replies |
|---|---:|---:|---:|---:|---:|---:|
| `nla_ar_contrast` | 49 | 4 | 4 | 1 / 1 | 59 | 11,708 |
| `nla_av_edit_ar` | 32 | 16 | 11 | 0 / 0 | 59 | 22,125 |
| `random_isotropic` | 12 | — | — | — | 12 | 2,528 |
| `none` | 7 | — | — | — | 7 | 910 |

Critical classification result:

- All currently reported dwelling/HOUSE, lion, and length whole-vector experiments are `nla_ar_contrast`, not `nla_av_edit_ar`.
- Canonical AV-edit-AR behavioral evidence is concentrated in emoji/style edits.
- The Wordle2 canonical substrate-edit length candidates were build-gated and never behaviorally swept.
- No archived result uses CAA or direct J-atom steering.
- All 82 saved-vector runs have verified archive and exact NPZ-member hashes;
  inline reconstructed directions remain recoverable by text/procedure rather
  than by an archived direction-array hash.

## 9. Explicit spec shape

New AR-pair example:

```yaml
method:
  schema: steering-taxonomy/v1
  root:
    method: nla_ar_contrast
    text_origin: authored
    contrast_relation: paired_descriptions
    edit_granularity: not_applicable
    pairing_style: matched_scaffold
  derivation:
    operator: none
    output_role: whole
  vector_view: unit_l2

design:
  schema: steering-study/v1
  intent: exploratory
  preregistered: false
  arm_role: treatment
```

Derived example:

```yaml
method:
  schema: steering-taxonomy/v1
  root:
    method: nla_av_edit_ar
    text_origin: av_generated_substrate
    contrast_relation: edited_vs_original
    edit_granularity: phrase
    pairing_style: not_applicable
  derivation:
    operator: jspace_sparse_nonnegative
    output_role: remainder
    parent_vector_key: g_phrase_glyph
    parameters:
      method: reference_inspired_greedy_nnls
      dictionary: full_vocabulary_j_lens_atoms
      candidate_pool: full_vocabulary
      atom_normalization: unit_l2
      solver: greedy_max_positive_correlation_with_nnls_refit
      paper_solver_relation: reference_inspired_divergence
      nonnegative: true
      k_requested: 25
  vector_view: unit_l2
```

## 10. Reporting contract

Every headline table should expose at least:

```text
Target | Root construction | Derivation/role | Vector view |
Intervention scope/scaling | Model/space | Evidence intent | N
```

Preferred terms:

- “NLA-mediated residual steering” only as a project umbrella followed by ancestry;
- “NLA AR text contrast” or `nla_ar_contrast`;
- “AV-edit-AR” or `nla_av_edit_ar`;
- “greedy-NNLS k25 J-atom approximation”;
- “k25 approximation remainder”;
- “k25 component-norm ratio,” with its formula;
- “direct J-lens atom” only for an actual atom intervention.

Avoid:

- generic “NLA vector” in result tables;
- “paper-exact” or “paper-faithful” decomposition;
- “sayable component,” “orthogonal residual,” “non-J residual,” or “dead/null remainder”;
- treating J-lens analysis as a fourth root construction;
- treating all-position injection as CAA.

## 11. Source-of-truth order

1. Immutable raw result JSON and vector/build artifacts.
2. Generated experiment manifest and registered residual spaces.
3. Current analysis/report code.
4. Campaign reports and steering ledger.
5. Historical preregistrations and handoff chronology.

Historical preregistrations should receive a status banner and terminology pointer, not silent retroactive rewriting.
