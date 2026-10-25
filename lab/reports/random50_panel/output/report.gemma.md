# Random 50-concept panel, gemma

800 held-out responses per method. Intervals: 20000 concept resamples, percentile, numpy default_rng(20260825).

| Method | Target word | 95% interval | Coverage | Quality | Lift/base | Lift/random |
|---|---:|---:|---:|---:|---:|---:|
| Unsteered | 4.5 | [2.6, 6.6] | 6.0 | 98.0 | 0.0 | n/a |
| Strongest random | 0.5 | n/a | 0.8 | 0.0 | -4.0 | n/a |
| Fixed CAA | 7.0 | [3.5, 11.4] | 9.5 | 32.8 | 2.5 | 6.5 |
| Calibrated CAA | 4.6 | [3.0, 6.4] | 7.3 | 90.5 | 0.1 | 4.1 |
| DiffMean | 10.3 | [6.1, 15.4] | 14.5 | 68.4 | 5.8 | 9.8 |
| NLA steering | 19.0 | [13.9, 24.4] | 24.0 | 83.9 | 15.5 | 16.9 |
| Direct prompting | 83.9 | [77.9, 89.3] | 89.5 | 87.0 | 79.4 | 83.4 |

| Paired difference across concepts | Mean (points) | 95% interval | Higher / lower / tied |
|---|---:|---:|---:|
| nla_minus_diffmean | 8.8 | [2.1, 15.0] | 27 / 10 / 13 |
| nla_gain_minus_diffmean_gain | 9.8 | [2.8, 16.0] | 30 / 10 / 10 |
| nla_minus_its_unsteered | 15.5 | [10.9, 20.4] | 30 / 2 / 18 |
| diffmean_minus_unsteered | 5.8 | [1.5, 11.1] | 21 / 10 / 19 |
| caa_fixed_minus_unsteered | 2.5 | [-1.3, 7.1] | 14 / 11 / 25 |
| caa_calibrated_minus_unsteered | 0.1 | [-1.8, 2.0] | 11 / 10 / 29 |
| prompting_minus_unsteered | 79.4 | [73.3, 85.1] | 50 / 0 / 0 |

Concepts passing the team's NLA confirmation rule: 6 of 50 [0, 44, 45, 179, 236, 245]

