# Track B results summary

Every benchmark we have run, with its metric, n, baseline, our number and a verdict. Losses
included.

**Verdict.**
- **Win:** better than the baseline beyond the stated interval or noise.
- **Tie:** within it.
- **Loss:** worse.
- **Pass / fail:** a check against a fixed bar rather than a baseline.

**Commit.** The code the number was produced with.
- `38ec284` is the squash of 9 commits (`9b4f1d8` ... `8b258a1`; the originals are on the local
  branch `backup/pre-squash`). A result recorded at any of those is from code now in `38ec284`.
- "batch" means this batch's uncommitted working tree.

**Stale.** Code that feeds the number has changed since; the change is named. Every table row is
also in `eval/results/results.parquet` unless marked "doc only".

## Payment-timing model

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| Kaggle invoices, Jul-Aug 2019 test | C-index | 5,955 | due date (0.601) | 0.741 (0.733-0.748) | win | `38ec284` (7a67bb3) |
| same | C-index | 5,955 | Beta-geometric only (0.735) | 0.741 | win (small; CIs overlap) | `38ec284` |
| same | C-index | 5,955 | Cox (0.770) | 0.741 | **loss** | `38ec284` |
| same | C-index | 5,955 | LightGBM person-period (0.754) | 0.741 | **loss** | `38ec284` |
| same | C-index | 5,955 | random survival forest (0.785) | 0.741 | **loss** | `38ec284` |
| same | integrated Brier, weeks 1-15 | 5,955 | due date (0.052) | 0.027 | win | `38ec284` |
| same | integrated Brier | 5,955 | LightGBM (0.026), forest (0.028) | 0.027 | tie | `38ec284` |
| same | calibration error at 4 weeks (ECE) | 5,955 | due date (0.050) | 0.028 | win | `38ec284` |
| same | ECE at 4 weeks | 5,955 | LightGBM (0.005), forest (0.007) | 0.028 | **loss** | `38ec284` |
| three-phase due-week variant | C-index | 5,955 | two-phase (0.735) | 0.721 | **loss** (dropped) | `27d37a1` |
| thin buyers (1-3 paid bills), generator, 30 worlds x 40 buyers, group prior alpha floored at 1.5 | MAE of p | 252 buyers | type prior only (0.0816) | group shrinkage 0.0847 | tie (+0.0031, CI -0.0003 to +0.0065) | batch |
| same | MAE of expected weeks to pay | 252 | type prior only (4.60) | 5.24 | **loss** (+0.65, CI +0.42 to +0.90) | batch |
| same, before the floor | MAE of p / weeks to pay | 252 | type prior only (0.0816 / 4.60) | 0.0851 / 7.96 | **loss** / **loss** | `7c3aebe` (stale: floor added) |
| heavy buyers (>= 20 paid bills) | MAE of p | 80 | type prior only (0.0532) | 0.0537 | tie | batch |

## Sales forecaster

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| Kaggle weekly counts, rolling one week ahead | mean log score, overall | 90 | Poisson (-6.54) | negative binomial -4.41 | win | `10c0c19` |
| same | mean log score, per customer | 3,364 | Poisson (-2.11) | -2.02 | win | `10c0c19` |
| same | 80% interval coverage, overall | 90 | target 0.80 | 0.97 | **fail** (too wide) | `10c0c19` |
| same | 80% interval coverage, per customer | 3,364 | target 0.80 | 0.86 | fail (slightly wide) | `10c0c19` |
| variability mechanisms on the calibration guard | mean \|gap\| p_short(90) | 100 pasts | both on (0.1422) | none 0.1484 | **loss**, inside the guard | `7c3aebe` (doc only) |
| parameter bootstrap restored, demand AR(1) still off (with the alpha floor) | mean gap / mean \|gap\| p_short(90) | 100 pasts, 4,000 paths | reference -0.033 / 0.133 | -0.0510 / 0.1472 | pass (guard 0.02): margins 0.0020 / 0.0058 | batch (doc only) |

The count model is unchanged since `10c0c19`. The demand AR(1) and demand level stay removed;
the parameter bootstrap is back on (batch), which changes paths but not the count fit.

## Categoriser

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| generator narrations, held-out formats | macro-F1 | 3,134 txns | rules + majority vote (0.991) | 0.992 | tie | batch (doc only) |
| same | macro-F1 | 3,134 | classifier alone (0.652) | 0.992 | win | batch |
| same | abstain share | 3,134 | rules alone (0.007) | 0.002 | win | batch |
| generator narrations, seen formats | macro-F1 | 3,134 | rules + majority vote (0.994) | 0.997 | tie | batch |
| label-model cascade before this batch (audit run) | macro-F1 held out | 3,134 | rules + majority vote (0.991) | 0.992 | tie | `38ec284` (doc only) |

Synthetic text only; no real bank narrations have been scored.

## Simulation engine

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| true-parameter mode vs oracle, 3 pasts x days 30/60/90 | p_short difference / standard error | 9 checks, 10,000 paths, 200 futures | within Monte Carlo error | max \|z\| 2.1 | pass | `38ec284` (doc only) |
| engine stream change (v1 to v2), same seeds | learned mean gap, mean \|gap\|, corr; breaking points | 100 pasts; 10,000 paths | v1 | within 1.1 standard errors | pass | `38ec284` (doc only) |
| learned mode vs oracle (calibration guard) | mean gap / mean \|gap\| / corr of p_short(90) | 100 pasts, 4,000 paths | reference -0.033 / 0.133 / 0.770 (ef5a591) | -0.0510 / 0.1472 / 0.739 | pass (guard 0.02, margin 0.0020 on the mean gap), **worse than reference** | batch (doc only) |
| same, by bin | predicted vs oracle p_short(90) | 100 pasts | diagonal | 0.05-0.20 bin: 0.126 vs 0.214 (n 28) | **fail** (low risk under-predicted) | batch |
| same, by bin | predicted vs oracle p_short(90) | 100 pasts | diagonal | 0.05-0.20 bin: 0.12 vs 0.19 | **fail** (low risk under-predicted) | `38ec284` |
| Berka loans, outside check | AUC for bad loans, p_short(90) | 682 loans (76 bad) | balance logistic regression (0.808) | 0.639 (0.558-0.716) | **loss** (-0.170) | `38ec284` (81890e9) |
| same | AUC, p_short(30) | 682 | balance logistic regression (0.808) | 0.658 | **loss** (-0.150) | `38ec284` |
| same | AUC, threshold = one loan payment | 682 | balance logistic regression (0.808) | 0.652 | **loss** (-0.156) | `38ec284` |
| same, with the quantile flow forecaster | AUC, p_short(90) | 682 | engine with bootstrap (0.639) | 0.587 | **loss** (paired -0.051, CI -0.088 to -0.014) | `38ec284` |

## Flow forecaster (unscheduled flows; off by default)

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| Berka accounts, 90 days | CRPS, daily | 300 accounts | same weekday last week (1,296) | 792 | win (0.61x) | `38ec284` |
| same | CRPS, daily | 300 | 8-week bootstrap (879) | 792 | win (0.90x, better in 96% of accounts) | `38ec284` |
| same | CRPS, 30-day sum | 300 | 8-week bootstrap (6,101) | 6,223 | tie (1.02x) | `38ec284` |
| same | CRPS, 90-day sum | 300 | 8-week bootstrap (13,587) | 12,635 | tie (0.93x, CI across 0) | `38ec284` |
| same | CRPS, 90-day sum | 300 | same weekday last week (50,369) | 12,635 | win (0.25x) | `38ec284` |

Data: the Kaggle copy of PKDD'99; on it the old outside-check numbers reproduce exactly
(`eval/data/SOURCES.md`).

## Calibration

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| 3 generator worlds, 7 days | 80% band coverage | 630 (overlapping) | engine band (0.733) | ACI gamma 0.05: 0.790 | win | `158b265` (stale) |
| same, 14 days | coverage | 630 | 0.716 | 0.778 | win | `158b265` (stale) |
| same, 30 days | coverage | 630 | 0.757 | 0.767 | tie | `158b265` (stale) |
| same, 60 / 90 days | coverage | 630 | 0.817 / 0.803 | ACI 0.665 / 0.575 | **loss**: production does not adapt these | `158b265` (stale) |
| same, all horizons | coverage | 630 | plain ACI | online step-size method | **loss** (removed in batch) | `158b265` |
| payment shock x 0.3, 14 days | weeks to recover coverage | 178 | engine band (never) | ACI 0.05: 5 | win (one world, illustrative) | `158b265` (stale) |

Stale: the engine changed since (empirical timing late-bin fix, sales variability removed). The
production path now calibrates 1-30 days (batch).

## Decision engine

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| 30 generator worlds | fresh-path p_short(90) | 30 worlds, 10,000 paths each | do nothing (0.501); discount all (0.387); chase largest (0.470) | 0.046 | win | `38ec284` (296f1da) |
| same | strict target hits (p_short <= 0.05) | 30 | solving at eps (5/30) | 24/30 at 0.8 eps | win; 6/30 still above target | `38ec284` |
| same | hits within Monte Carlo error | 30 | target | 30/30 | pass | `38ec284` |
| same | mean plan cost | 30 | discount all Rs 7,222; chase Rs 500 | Rs 61,899 | dearer (buys the risk cut) | `38ec284` |
| same | solver at time limit | 30 | none | 5/30 (max gap 0.87) | **fail** (not proven optimal) | `38ec284` |
| chase-column trim | mean plan cost | 30 | default (Rs 61,899) | trimmed Rs 65,520 | **loss** (+3,621, CI +975 to +7,303): trim rejected | batch |
| MSMED claims ("money owed") | invoices worth claiming at defaults | 1,453 overdue | | 0 | (finding) | `38ec284` |
| CVaR plan vs chance-constrained (alpha 0.05, budget Rs 0), same 30 worlds and paths | owner target met strictly on fresh paths (p_short(90) <= 0.05) | 30 worlds | chance-constrained 19/30 | CVaR 30/30 | win: CVaR is the owner-view default | batch |
| same | mean plan cost | 30 | chance-constrained Rs 54,106 | CVaR Rs 1,15,801 | **dearer** (+Rs 61,695, CI +49,676 to +73,543; 30/30 worlds) | batch |
| same | fresh p_short(90) / expected shortfall over all futures / worst-5% average shortfall | 30 | 0.0467 / Rs 30,701 / Rs 6,12,092 | 0.0075 / Rs 4,811 / Rs 96,224 | win | batch |
| same | solve time median (max); solver at time limit | 30 | 31.1 s (60.1 s); 4/30 | 1.4 s (4.2 s); 0/30 | win | batch |
| same | CVaR budget met in sample | 30 | | 7/30 (23/30 have paths no action saves; fallback: lowest CVaR, then cheapest within 0.1%) | (finding) | batch |
| stress: payment chances x 0.85, order rate x 0.9, plans not re-solved | p_short(90) rise / worst-5% rise | 30 | chance-constrained +0.0670 / +Rs 7,41,059 | CVaR +0.0205 / +Rs 2,82,351 | win (CVaR meets target 27/30 stressed, chance 0/30) | batch |

## Attribution (batch)

| check | result |
|---|---|
| driver with no effect | contributes exactly 0: pass |
| planted late buyer (late throughout the history) | ranks first among buyers: pass |
| planted late buyer (late only for the last 6 months) | not first: **fail**. The timing model does not detect the change (no change-point detection) |
| determinism | identical: pass |
| top 5 across 3 seeds | ranks 1-4 identical; 5 and 6 swap within Monte Carlo error: pass on the stated rule, not exact |
| time | 62 s at 10,000 paths, about 20 s at 4,000 (default world, 113 drivers) |

## Naive vs ours (demo evidence, batch)

`eval/models/naive_vs_ours.py`, `eval/results/naive_vs_ours.md`. Naive = the same engine with every open
bill paid on its due date (no learned timing, no never-paid share), no future sales or linked purchases
(supplier payments as a flat daily outflow at their historical average), no sector shocks, no parameter
uncertainty, no calibration, no plan. Both are scored against the oracle (100 true futures per world).
Setup: 30 worlds (presets x past seeds 0-9), threshold 0, 10,000 paths. Paired bootstrap over worlds.

| metric | n | naive | ours | ours - naive (95% CI) | verdict |
|---|---|---|---|---|---|
| median balance error, day 30 (Rs lakh) | 30 | 84.71 | 3.77 | -80.95 (-91.70 to -69.27) | win (30/30 worlds) |
| median balance error, day 60 | 30 | 44.36 | 6.73 | -37.63 (-45.36 to -30.02) | win (30/30) |
| median balance error, day 90 | 30 | 24.80 | 10.30 | -14.50 (-22.07 to -8.13) | win (25/30) |
| p_short(90) error | 30 | 0.267 | 0.089 | -0.178 (-0.261 to -0.104) | win (26/30) |
| flags a real shortfall (p >= 0.5): caught / false alarms | 14 real | 10/14 / 1 | 11/14 / 0 | | win (small) |
| same at p >= 0.2 | 20 real | 11/20 / 0 | 16/20 / 0 | | win |

**Why naive's error is so large early on.** It counts every overdue bill as cash on day 1. Its error
shrinks by day 90 only because the missing future sales and purchases partly offset that.

**Demo world** (borderline, past seed 0, Rs 10 lakh cushion; the true 90-day chance is 0.46):

| | 90-day chance | ours (Rs lakh) | naive (Rs lakh) | truth (Rs lakh) |
|---|---|---|---|---|
| truth | 0.460 | | | |
| ours | 0.327 | | | |
| naive | 0.000 | | | |
| median day 30 | | 37.62 | 128.53 | 34.46 |
| median day 90 | | 52.23 | 36.31 | 45.01 |

- **Dead bills:** naive counts 20 bills (Rs 40.69 lakh) as certain cash that ours marks as likely never
  paid (chance >= 0.5). In truth, 19 of the 20 are never paid.
- **Ours still under-predicts:** 0.33 against the true 0.46, the same low-risk under-prediction the
  calibration guard bins show.

Chart data: `eval/demo/naive_vs_ours.json`.

## Generator

| benchmark | metric | n | baseline (value) | ours | verdict | commit |
|---|---|---|---|---|---|---|
| generator vs Kaggle invoices | AUC of a classifier telling them apart | 28,087 invoices | 0.5 (indistinguishable) | 0.999-1.000 | **fail**: generator invoices are not realistic | `10c0c19` |

## Learning-curve replay (simulation engine, day-by-day)

Three borderline worlds (past seeds 0-2), 60 daily steps each, 10,000 paths. Each day the tables
known by then are revealed and the normal pipeline runs; its median is scored against the realised
balance h days later. Baseline: flat, today's balance carried forward. n = 180 (60 per world; 90
days: 90). Consecutive days overlap, so n overstates independent evidence. Re-run in this batch;
the old `7aca3b1` numbers are in brackets.

| horizon | MAE, Rs lakh (old) | flat MAE | ratio | verdict | 50% band coverage | 90% band coverage |
|---|---|---|---|---|---|---|
| 1 day | 2.39 (2.35) | 2.43 | 0.99 | tie | 0.46 | 0.95 |
| 7 days | 6.22 (6.15) | 8.20 | 0.76 | win | 0.48 | 0.92 |
| 14 days | 9.07 (9.07) | 12.08 | 0.75 | win | 0.46 | 0.93 |
| 30 days | 11.08 (11.28) | 15.22 | 0.73 | win | 0.51 | 0.93 |
| 60 days | 16.02 (16.19) | 16.96 | 0.94 | tie | 0.49 | 0.82 |
| 90 days | 16.93 (16.62) | 12.87 | 1.32 | **loss** | 0.58 | 0.80 |

The 90% band covers 0.92-0.95 up to 30 days (slightly wide) and 0.80-0.82 at 60-90 days (too
narrow). At 90 days the median is biased up by Rs 9.1 lakh and loses to the flat baseline.
These are the engine's own bands; the calibration component is not applied in the replay runner.

Payment-timing range for each world's most-invoiced buyer (weeks to pay once due, 1/p, 90% range):

| world | truth | day 1 range (labels) | day 60 range (labels) | truth inside on day 60 |
|---|---|---|---|---|
| 0 | 6.61 | 4.55-7.15 (74) | 4.89-7.15 (87) | yes |
| 1 | 3.42 | 3.34-4.96 (85) | 3.34-4.89 (103) | yes |
| 2 | 6.43 | 6.06-9.26 (55) | 6.36-9.69 (63) | yes |

The ranges barely narrow over 60 days (13-18 extra labels on 55-85).

## World 0 diagnoses

World 0 is the default world (borderline preset, past seed 0) that most single-world checks use.
- **Replay, world 0 alone** (n = 60 per horizon; 90 days n = 30):

  | horizon | median MAE (Rs lakh) | flat MAE | 90% band coverage |
  |---|---|---|---|
  | 1 day | 3.04 | 2.76 | 0.92 |
  | 7 days | 6.26 | 9.15 | 0.88 |
  | 14 days | 7.98 | 13.55 | 0.95 |
  | 30 days | 13.97 | 22.40 | 0.93 |
  | 60 days | 23.29 | 26.97 | 0.70 |
  | 90 days | 34.78 | 9.95 | 0.40 |

  The 90-day loss comes mostly from this world: its median is Rs 34.8 lakh too high at 90 days.
  World 1 is better than flat at 90 days, world 2 at every horizon.
- **Payment-timing late-bin diagnosis** (`38ec284`): before the fix, open invoices 4 or more
  weeks past due (Rs 63.9 lakh) paid Rs 11.9 lakh by day 30 in the engine, against Rs 6.9 lakh
  with the hazards off. Cause: hazards in the empty and end bins. After the fix the learned-mode
  receipts gap at day 30 fell from +Rs 7.7 lakh to +Rs 0.6 lakh.
- **Decision engine** (Rs 20 lakh cushion; `38ec284`): plan p_short(90) 0.045 on fresh paths
  against 0.707 doing nothing, at Rs 22,743. Rs 7 lakh overdraft on day 1, S2 stretched 30 days,
  two chases, four TReDS discounts.
- **Attribution** (Rs 20 lakh cushion, 10,000 paths, batch): the top 10 drivers are all overdue
  invoices. The largest (also the main MSMED-overdue invoice) would cut p_short(90) by 0.50 if
  paid on day 1.
- **Planted slowdown** (batch): a buyer that slows to a tenth of its chance six months before
  as_of keeps an estimated p of 0.169 against a true 0.015 since the change. No change-point
  detection, so neither the forecast nor attribution sees it.
