# External assets

The repository is self-contained for source inspection, archived-result table
and figure regeneration, numerical regression checks, and the offline
prompting-quality reproduction. Full model-generation experiments additionally
need the following assets, which are not copied into Git because of size and
their upstream licenses.

| Role | Identifier | Required layer |
|---|---|---:|
| Qwen target model | `Qwen/Qwen2.5-7B-Instruct` | 20 |
| Qwen NLA activation-vector checkpoint | `kitft/nla-qwen2.5-7b-L20-av` | 20 |
| Qwen NLA activation-reconstruction checkpoint | `kitft/nla-qwen2.5-7b-L20-ar` | 20 |
| Gemma target model | `google/gemma-3-12b-it` | 32 |
| Gemma NLA activation-vector checkpoint | `kitft/nla-gemma3-12b-L32-av` | 32 |
| Gemma NLA activation-reconstruction checkpoint | `kitft/nla-gemma3-12b-L32-ar` | 32 |

Copy `configs/models.example.yaml` to the ignored `configs/models.yaml` and
replace identifiers with approved local paths when required. Access to Gemma
may require accepting its upstream license. API-only judge reruns require an
`OPENROUTER_API_KEY`; no credentials belong in this repository.
