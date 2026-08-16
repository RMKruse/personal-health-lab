# V0.4 weight-model calibration prototype

Version: `v0.4-weight-model-calibration-1` · Mode: `full`

PROTOTYPE — synthetic evidence only; real personal data require V0.5 revalidation.

## Candidate definition for HITL review

| Window | Energy Ridge | Energy + macro Ridge |
|---:|---:|---:|
| 7 days | 100 | 100 |
| 14 days | 100 | 100 |
| 30 days | 100 | 100 |
| 90 days | 30 | 1 |

- Primary block rule per window: `max(ceil(window/3), ceil(1 x n_weight^(1/3)))` observed weights.
- Required bootstrap: 2,000 successful refits in at most 2,020 attempts.
- Synthetic absolute bias floors and family multipliers: `{'absolute_floors': {'7:energy': 0.05951901852797563, '14:energy': 0.04347286163959035, '30:energy': 0.04413487736489694, '90:energy': 0.04571668150809829, '7:energy_macro': 0.13223244620115282, '14:energy_macro': 0.1424485509244905, '30:energy_macro': 0.19712251806051034, '90:energy_macro': 0.10516808790176849}, 'family_multipliers': {'energy': 1.4544141140865294, 'energy_macro': 1.120367220739441}}`.
- Trend: the already-decided triangular local-linear fit, bandwidth = window, truncated edge support.
- Missingness: only common `complete` nutrition/activity/resting-energy days contribute; partial values remain excluded and unfilled.

## Independent synthetic holdout after maturity gate

| Family | Cases | Bias | RMSE | Pointwise coverage | Simultaneous coverage | Null false alarm | Prediction skill |
|---|---:|---:|---:|---:|---:|---:|---:|
| energy | 23 | 0.0001 | 0.0136 | 96.4% | 95.7% | 0.0% | 0.067 |
| energy_macro | 23 | 0.0003 | 0.0229 | 98.9% | 91.3% | 0.0% | 0.077 |

## Candidate maturity gate

- `minimum_common_coverage`: `0.35`
- `maximum_common_gap_days`: `35.0`
- `minimum_anchors`: `100.0`
- `maximum_unpenalized_condition`: `1000.0`
- `maximum_residual_acf`: `0.8`
- `maximum_ridge_sensitivity`: `0.025`
- `minimum_time_blocked_prediction_skill`: `-0.5`
- `maximum_mean_band_half_width`: `0.25`

## Bootstrap stability and dependency

- Full stability probe: 8,000 successes, 0 failures.
- Mean-halfwidth p95 prefix changes: `{'250': 0.0, '500': 0.0, '1000': 0.0}`.
- Neighbor-block p95 mean-halfwidth change: 0.0000 kg/week per personal SD.
- NumPy completed all final linear algebra and bootstrap work; no additional statistics dependency was needed.

## Synthetic-data boundary

These gates measure performance inside the versioned generator, not validity on personal data. Until V0.5 compares real coverage, collinearity, autocorrelation, gaps, source changes, and error proxies with this envelope, real outputs must remain `exploratory`; mismatches require extending the synthetic scenarios and recalibrating the whole definition, never tuning thresholds to the observed result.
