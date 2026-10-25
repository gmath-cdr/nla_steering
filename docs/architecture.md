# Experiment architecture

```mermaid
flowchart LR
  A[AxBench concepts and prompts] --> B[Frozen panel manifests]
  B --> C[NLA AV-edit evaluation<br/>Qwen L20 / Gemma L32]
  B --> D[Fixed-direction baselines<br/>CAA, DiffMean, SAE, LoReFT]
  C --> E[Saved completions and AV/AR transcripts]
  D --> E
  E --> F[Concept-word incidence and matched quality checks]
  E --> G[Prompting-quality re-score]
  E --> H[Unembedding and J-space diagnostics]
  E --> R[Two-judge rubric and matched factuality]
  F --> I[CSV, Markdown, and PNG outputs]
  G --> I
  H --> I
  R --> I
  I --> J[Reported tables and result summaries]
```

The important data boundary is between reproducible source/configuration and
large external assets. Source, frozen configurations, saved completions, result
summaries, and compact vectors are versioned. Base models, NLA checkpoints,
raw generation caches, and API credentials are acquired separately.

The archived-analysis path emits canonical CSV tables, reviewable Markdown,
and high-resolution PNG copies of every table and figure.
