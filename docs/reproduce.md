# Reproduction guide

This guide separates offline checks over saved outputs from model-dependent
reruns. Source revisions are fixed in [`../sources.lock`](../sources.lock).

## 1. Verify the checkout offline

```bash
python3 run.py self-test
```

The self-test supports Python 3.10--3.13 and uses only the standard library. It:

1. checks the vendored source layout;
2. authenticates compact release inputs with
   `artifacts/release/sha256sums.txt`;
3. validates the result mapping and all files listed in
   `artifacts/paper_runs/paper-sha256sums.txt`;
4. asserts 188 frozen numerical expectations across 13 table families;
5. verifies the complete AxB44 and emoji archives, all three selected-panel
   judge checkpoints, and all 34 Wordle result files;
6. rebuilds the Qwen panel-14, Gemma panel-14, and Gemma panel-10 prompting
   quality results.

Expected prompting checks are 79.0%, 84.8%, and 81.2%, respectively. The two
Gemma panels contain 224 and 160 rows and must not be substituted.

To verify only the archived result snapshot:

```bash
python3 run.py verify-paper
```

To verify or rebuild only the four later study families:

```bash
python3 run.py verify-studies
python3 run.py reproduce-studies
uv run --locked --extra analysis python3 run.py reproduce-studies --figures
```

These commands write generated products only under
`artifacts/rebuilt/extended_studies/`; the authenticated inputs remain read-only.

To recompute the archived aggregates and compare all 188 frozen values without
rendering presentation outputs:

```bash
python3 run.py compare-results
```

See [`../artifacts/paper_runs/paper-results.md`](../artifacts/paper_runs/paper-results.md)
for the human-readable coverage index.

## 2. Install the locked analysis environment

```bash
./scripts/bootstrap.sh
uv lock --check
```

The bootstrap installs the exact transitive versions and hashes in `uv.lock`
and reruns the offline test.

## 3. Rebuild archived tables and figures

```bash
uv run --locked --extra analysis ./run.py reproduce
```

Outputs are written under `artifacts/rebuilt/paper/`:

- 13 canonical CSV tables;
- 13 reviewable Markdown tables;
- 13 high-resolution PNG table renderings;
- 11 high-resolution PNG figures;
- one machine-readable `metrics.json`.

To regenerate only tables:

```bash
uv run --locked --extra analysis ./run.py reproduce --tables-only
```

Every table can be rebuilt independently:

```bash
for name in \
  tab_panel_full tab_gemma_independent tab_discovery500_summary \
  tab_confirmation30_all tab_unembedding_per_concept tab_sae_sweeps \
  tab_reft_panel14 tab_axbench_rescore tab_position tab_reframing \
  tab_factual_three_arm tab_gemma10_quality tab_gemma_six; do
  uv run --locked --extra analysis ./run.py paper-table "$name"
done
```

CSV is the numerical source of truth. PNG is provided for GitHub, Google Docs,
and presentation software.

## 4. Configure external models

```bash
cp configs/models.example.yaml configs/models.yaml
```

Replace the template entries with accessible local paths or approved model
identifiers. Full generation requires:

- Qwen2.5-7B-Instruct with layer-20 NLA AV and AR checkpoints;
- Gemma-3-12B-it with layer-32 NLA AV and AR checkpoints.

The checkout does not redistribute model weights. Gemma may require accepting
its upstream license.

Install the complete locked experiment environment:

```bash
uv sync --locked --extra experiments
eval "$(./run.py print-pythonpath)"
```

## 5. Re-score prompting quality without a GPU

```bash
python3 run.py rescore-prompting \
  --unsteered artifacts/release/prompting_quality/panel14_qwen_matched_panel.json \
  --prompting artifacts/release/prompting_quality/panel14_qwen_prompting.json \
  --out-dir artifacts/rebuilt/panel14_qwen_prompting
```

The scorer joins responses by `(concept_id, prompt_index)`, recomputes
content-word Jaccard similarity, and reapplies the frozen length, repetition,
and similarity checks.

## 6. Re-run fixed panel baselines

After the model paths and inference services are configured:

```bash
uv run --locked --extra experiments ./run.py panel-baselines \
  --model-key qwen \
  --protocol src/nla/experiments/configs/qwen_selected14_baseline_protocol.json \
  --output artifacts/rebuilt/panel14_qwen.json

uv run --locked --extra experiments ./run.py panel-baselines \
  --model-key gemma \
  --protocol src/nla/experiments/configs/qwen_selected14_baseline_protocol.json \
  --output artifacts/rebuilt/panel14_gemma_transfer.json

uv run --locked --extra experiments ./run.py panel-baselines \
  --model-key gemma \
  --protocol src/nla/experiments/configs/gemma_confirmed10_baseline_protocol.json \
  --output artifacts/rebuilt/panel10_gemma.json
```

Never tune on held-out prompts or replace one confirmed panel with another.

## 7. API reliability analyses

The archived snapshot includes the saved judge outputs used by the analysis.
Fresh judge calls need an `OPENROUTER_API_KEY` and may vary with provider or
model revisions. Relevant scripts are under `src/nla/experiments/`, including
`rubric_two_judge.py` and `factuality_three_arm.py`.

## 8. Compute and scope

Offline verification and archived analysis use CPU only. The four later
generation campaigns record elapsed time and GPU identity in every saved result.
Summing those authenticated result fields gives:

| Campaign | Model | Saved generations | Recorded active-spec time |
|---|---|---:|---:|
| Chosen-in-advance AxB44 | Qwen2.5-7B | 1,994 | 1.31 A100-hours |
| Chosen-in-advance AxB44 | Gemma-3-12B | 1,994 | 3.67 A100-hours |
| Emoji | Qwen2.5-7B | 4,026 | 4.74 A100-hours |
| Emoji | Gemma-3-12B | 2,992 | 9.78 A100-hours |
| **Total** | | **11,006** | **19.49 A100-hours** |

All four campaigns used one NVIDIA A100-SXM4-80GB. The recorded time is the
sum of per-result `runtime.elapsed_seconds`, including saved canary,
calibration, and evaluation runs. It is not a billing total because it excludes
model staging, direction construction, API judging, and idle or resume gaps.
The archives record zero network model downloads during generation. The
selected-panel judge council used 32,256 external API calls and no new model
generations.

The earlier Wordle files and the original 500-concept discovery do not contain
complete runtime receipts, so their GPU hours cannot be reconstructed honestly.

The checkout contains the code and saved data needed for all 21 bundled result
groups. The four later AxB44, judge-council, emoji, and Wordle families are
verified with `python run.py verify-studies`. The localized translation-edit
study remains outside the self-contained scope until a collaborator's authoritative
scripts and raw generation logs are supplied.
