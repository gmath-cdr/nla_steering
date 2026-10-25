# Qwen-selected 14-concept steering comparison

Gemma rows are transfer results on the Qwen-confirmed panel.

| Model | Method | N | Hit | Coverage | Quality | Lift/base | Lift/random |
|---|---|---:|---:|---:|---:|---:|---:|
| qwen | caa_calibrated | 224 | 11.6% | 17.9% | 96.4% | 5.8% | 3.6% |
| qwen | caa_fixed | 224 | 15.2% | 21.4% | 96.0% | 9.4% | 7.1% |
| qwen | diffmean | 224 | 17.4% | 24.1% | 95.1% | 11.6% | 9.4% |
| qwen | unsteered | 224 | 5.8% | 8.0% | 98.2% | 0.0% | — |
| qwen | strongest_random | 224 | 8.0% | 10.7% | 96.4% | 2.2% | — |
| qwen | nla_av_edit | 224 | 44.2% | 58.0% | 81.2% | 38.8% | 33.9% |
| gemma | caa_calibrated | 224 | 7.6% | 10.7% | 85.7% | 4.9% | 6.7% |
| gemma | caa_fixed | 224 | 10.7% | 17.0% | 24.1% | 8.0% | 9.8% |
| gemma | diffmean | 224 | 10.3% | 13.4% | 60.7% | 7.6% | 9.4% |
| gemma | unsteered | 224 | 2.7% | 4.5% | 98.2% | 0.0% | — |
| gemma | strongest_random | 224 | 0.9% | 1.8% | 0.0% | -1.8% | — |
| gemma | nla_av_edit | 224 | 28.1% | 36.6% | 79.5% | 27.2% | 25.4% |
