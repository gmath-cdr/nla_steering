# Independent Gemma replication

Both models advanced 30 candidates: Qwen retained 14 and Gemma retained 10.
Panels differ; within each panel all methods share concepts and held-out protocol.

| Model | Panel | Method | Strict hit, 95% CI |
|---|---|---|---:|
| Qwen2.5-7B | Qwen-selected 14 | Unsteered | 5.8 [2.7, 9.4] |
| Qwen2.5-7B | Qwen-selected 14 | Fixed CAA | 15.2 [7.6, 23.2] |
| Qwen2.5-7B | Qwen-selected 14 | Calibrated CAA | 11.6 [4.5, 20.5] |
| Qwen2.5-7B | Qwen-selected 14 | DiffMean | 17.4 [8.9, 26.3] |
| Qwen2.5-7B | Qwen-selected 14 | NLA AV edit | 44.2 [32.6, 55.4] |
| Qwen2.5-7B | Qwen-selected 14 | SAE | 7.1 [3.1, 12.1] |
| Qwen2.5-7B | Qwen-selected 14 | LoReFT | 18.8 [11.6, 26.3] |
| Qwen2.5-7B | Qwen-selected 14 | Direct prompting | 80.8 [66.1, 91.1] |
| Gemma-3-12B | Gemma-selected 10 | Unsteered | 5.0 [0.6, 10.0] |
| Gemma-3-12B | Gemma-selected 10 | Fixed CAA | 9.4 [1.9, 18.1] |
| Gemma-3-12B | Gemma-selected 10 | Calibrated CAA | 6.9 [1.2, 14.4] |
| Gemma-3-12B | Gemma-selected 10 | DiffMean | 11.9 [1.2, 30.6] |
| Gemma-3-12B | Gemma-selected 10 | NLA AV edit | 38.8 [28.7, 48.8] |
| Gemma-3-12B | Gemma-selected 10 | SAE | 3.1 [0.0, 6.9] |
| Gemma-3-12B | Gemma-selected 10 | LoReFT | 10.0 [3.8, 16.9] |
| Gemma-3-12B | Gemma-selected 10 | Direct prompting | 93.8 [88.1, 98.8] |
