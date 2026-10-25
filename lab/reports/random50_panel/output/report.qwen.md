# Random 50-concept panel, qwen

800 held-out responses per method. Intervals: 20000 concept resamples, percentile, numpy default_rng(20260825).

| Method | Target word | 95% interval | Coverage | Quality | Lift/base | Lift/random |
|---|---:|---:|---:|---:|---:|---:|
| Unsteered | 7.4 | [4.8, 10.1] | 10.3 | 96.9 | 0.0 | n/a |
| Strongest random | 6.3 | n/a | 8.0 | 96.0 | -1.1 | n/a |
| Fixed CAA | 9.0 | [5.3, 13.4] | 12.0 | 93.8 | 1.6 | 2.8 |
| Calibrated CAA | 9.5 | [5.5, 14.1] | 12.5 | 94.6 | 2.1 | 3.3 |
| DiffMean | 12.8 | [8.3, 17.8] | 17.3 | 94.6 | 5.4 | 6.5 |
| NLA steering | 25.1 | [18.5, 32.0] | 33.3 | 83.0 | 18.8 | 15.5 |
| Direct prompting | 78.0 | [71.1, 84.3] | 86.3 | 78.1 | 70.6 | 71.8 |

| Paired difference across concepts | Mean (points) | 95% interval | Higher / lower / tied |
|---|---:|---:|---:|
| nla_minus_diffmean | 12.4 | [6.1, 18.9] | 26 / 6 / 18 |
| nla_gain_minus_diffmean_gain | 13.4 | [7.0, 20.0] | 28 / 8 / 14 |
| nla_minus_its_unsteered | 18.8 | [13.3, 24.5] | 34 / 2 / 14 |
| diffmean_minus_unsteered | 5.4 | [2.0, 9.1] | 16 / 7 / 27 |
| caa_fixed_minus_unsteered | 1.6 | [-1.3, 4.9] | 13 / 9 / 28 |
| caa_calibrated_minus_unsteered | 2.1 | [-0.9, 5.8] | 9 / 8 / 33 |
| prompting_minus_unsteered | 70.6 | [63.4, 77.4] | 50 / 0 / 0 |

Concepts passing the team's NLA confirmation rule: 4 of 50 [0, 387, 418, 422]

