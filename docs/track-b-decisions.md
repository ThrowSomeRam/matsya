# Track B decisions

Decisions the intelligence plane is built on. Components are referred to by name.
Constants and their sources live in `intelligence/config/track_b_constants.yaml`.

## Standing rules

1. **Each flow's date and amount each have exactly one owner.** No cash-flow category is
   forecast by two components, and no component second-guesses another's output.
2. **Overrides never update learned parameters; only corrections (facts) do.** An owner saying
   "assume buyer X pays in 60 days" changes this run's simulation inputs. An owner saying
   "this transaction is rent, not a supplier payment" is a fact and retrains the categoriser.
3. **Scenario comparisons use the same seed** (common random numbers), so a difference between
   two runs is the scenario, not sampling noise.
4. **Runs store their override list, not just a hash,** so any run can be replayed exactly.

### Who owns each flow

| Flow | Date owner | Amount owner |
|---|---|---|
| Receipts on existing invoices | payment-timing model | invoice (gross minus expected TDS) |
| Receipts on future invoices | sales forecaster (issue date) then payment-timing model | sales forecaster |
| Purchases | sales forecaster (purchase lag before issue) | sales forecaster (purchase ratio) |
| Fixed obligations (salary, rent, EMI, fees, PF, ESI, TDS deposit) | obligations detector | obligations detector |
| GST | obligations detector (statutory calendar) | sales forecaster: gst_rate x (taxable sales - taxable purchases) for the period, credit carried forward |
| Unscheduled flows (refunds, misc income, cash, owner transfers, bank charges) | flow forecaster | flow forecaster |

In the obligations output, `amount_owner` says who owns each row's amount. GST rows carry the
obligations detector's date with `amount_paise` null and `amount_owner = "sales_forecaster"`;
the simulation engine fills the amount per path from `sales.gst_by_month`. Supplier payments
never appear in the obligations output, even when perfectly regular.

Known limits of the sales forecaster: weekly counts are sampled independently (demand
autocorrelation is not modelled), and when purchase-to-invoice lags vary by a few days the
dispersion estimate is biased upward (counts smear across week boundaries), so weekly sales
look less lumpy than they are.

The synthetic generator tags every true transaction with its generating process (`sales`,
`purchase`, `fixed_obligation`, `unscheduled`) so tests can check this mapping.

## Shortfall definitions

Let $B^{(n)}_t$ be the simulated closing balance on day t after as_of in path n, N paths, and
$\theta$ the shortfall threshold (`shortfall_threshold_paise`, default 0; later
$-\text{overdraft limit} + \text{owner cushion}$).

- **Shortfall:** the balance is below the threshold, $B_t < \theta$.
- **Probability of shortfall by day t:** the share of paths whose balance has been below the
  threshold on any day up to t (ever, not on day t):
  $$p_{\text{short}}(t) = \frac{1}{N}\sum_n \mathbf{1}\Big[\min_{s \le t} B^{(n)}_s < \theta\Big]$$
  It never decreases with t.
- **Expected shortfall by day t:** the average depth of the worst dip below the threshold, over
  paths that breach by t:
  $$\text{ES}(t) = \mathbb{E}\Big[\theta - \min_{s \le t} B_s \;\Big|\; \min_{s \le t} B_s < \theta\Big]$$
  Reported with $p_{\text{short}}(t)$ so the two together give the unconditional expected
  depth $p_{\text{short}}(t)\,\text{ES}(t)$.
- **Runway:** the first day t at which $p_{\text{short}}(t)$ passes 0.5. Undefined (reported
  as "beyond horizon") if it never does within 90 days.
- **Fan chart:** per day, the 5th, 25th, 50th, 75th and 95th percentiles of $B_t$ across paths
  (marginal per day, not a single path).

All of these are computed on the same paths; the oracle versions in the synthetic generator
(`Futures.oracle_shortfall`) use resampled true futures of one fixed past.

## Contracts v0.1 integration (2026-10-24)

Track B now reads and writes only contract tables (`matsya_contracts`).

- **Types.** `intelligence/io/types.py` takes every contract schema from `matsya_contracts`;
  the only shapes defined there are the adapters' internal outputs.
- **Adapters** (`io/adapters.py`): as-of reads (per key, latest row with known_at <= as_of)
  of ledger.balances, ledger.invoices, ledger.matches and labels.invoice_outcomes;
  transactions with known_at <= as_of; effective sector from ref.buyers + ref.tenant_buyers;
  owner corrections from labels.txn_corrections (latest corrected_at wins). An as_of given as
  a date means the end of that day, UTC. Models are unchanged.
- **Payment-timing labels** use the as-of read and each label's censored_at. The old
  `as_of - 3 days` cut (label_lag_days) is gone; open invoices are known unpaid through
  as_of, so the engine's survival runs to day 0.
- **TReDS.** An invoice with an as-of match whose payer_kind is treds_financier is censored
  for timing at the discount date (earliest such credit's value_date), whatever its label
  row says. Its cash stays in the ledger and the invoice counts as settled (not open in the
  engine).
- **Terms.** terms_source unknown: terms and MSMED deadline 45 days
  (`msmed_act.section_15.unknown_terms_default_days`, status decide). default_15d: 15.
  Stated terms: the stated days, MSMED deadline capped at 45.
- **Opening balance** is the latest ledger.balances row known by as_of. The generator now
  writes the AA summary balance as a bank_reported row at as_of, which is the opening-balance
  fix proposed in the calibration diagnosis.
- **Outputs**, each validated before writing (`io/sink.py`, refuses non-Track-B tables):
  features.txn_category (categoriser), features.obligations (obligations detector),
  features.invoice_timing (payment-timing model), features.sales_process (sales
  forecaster), forecast.shock_state (shock component), forecast.runs,
  forecast.balance_quantiles, forecast.shortfall (simulation engine). The engine's shortfall
  columns are renamed to `expected_shortfall_cond_paise` / `expected_shortfall_uncond_paise`.
- **Data source.** `intelligence/config/data_source.yaml`, one value `DATA_SOURCE`
  (`synth` | `contract`, env `MATSYA_DATA_SOURCE`). `python -m matsya_intelligence.run`
  runs every tenant and writes every table. A test checks the same world gives
  bit-identical outputs from either source.
- **Generator.** Contract IDs (UUIDv4 tenant, txn_uid from a synthetic bank txnId, invoice_id
  from supplier GSTIN + invoice number + FY, buyer_id from PAN), append-only invoice and label
  versions, matches, ref.buyers / ref.tenant_buyers. New knobs, off by default:
  `terms_unknown_share`, `treds_discount_prob` (CPSE invoices discounted 2-7 days after issue,
  fee `treds_fee`). `synth.export.write_contract_parquet(world, out_dir)` writes every
  contract table plus truth tables in `out_dir/truth/`.
- **CI.** `.github/workflows/track-b.yml` runs the contract conformance tests and the Track B
  unit tests.

**Effect on learned-mode calibration** (same 100 pasts and oracle cache as above, all fixes
on, base logit on):

| variant | mean gap | mean \|gap\| | corr | 0.05-0.20 pred / oracle | 0.40-0.60 | median gap day 30 / 90 (Rs) | receipts gap day 90 (Rs) |
|---|---|---|---|---|---|---|---|
| before contracts (3-day label cut, reconstructed opening) | +0.021 | 0.143 | 0.738 | 0.136 / 0.193 | 0.494 / 0.356 | -3,01,976 / -4,16,672 | -1,91,601 |
| contracts (as-of labels, bank-reported opening) | -0.039 | 0.141 | 0.753 | 0.125 / 0.198 | 0.470 / 0.475 | -29,663 / +30,364 | +88,854 |

The median balance bias mostly goes, from the bank-reported opening balance. Receipts are now
over-forecast: payments made before as_of but reported after it are in the bank balance, and
their invoices still look open, so they are counted twice. That is question 1 below (a
complete-through date would fix it). The low bin is still under-predicted.

### Questions for Track A (contracts v0.1)

1. **censored_at for open labels** (proposal: the watermark rule below). The fixtures stamp it with the as_of date. A payment
   value-dated before as_of but reported after it is invisible at as_of, so the last days
   before as_of count as unpaid when they were not. This biases p down slightly near the
   cut (the reason for the old 3-day lag). Can censored_at be the date through which the
   account's ledger is complete (e.g. the AA data range end minus reporting lag), or can
   ledger.balances / a new column carry that date?
2. **Are open labels re-stamped at every fetch?** The as-of read takes the latest label row;
   if an open invoice only has its issue-time row, censored_at stays at the issue date and
   the label loses every week since.
3. **Does every invoice get a label row from issue?** The adapters assume one row per invoice
   in labels.invoice_outcomes.
4. **Balance precedence.** The as-of read per (tenant, account, date) takes the latest
   known_at whatever the balance_source. Is that intended when a bank_reported and a
   reconstructed row exist for the same date? Will A write a bank_reported row from the AA
   summary balance at each fetch? (It is the opening-balance fix.)
5. **due_date when terms_source is unknown.** The fixtures write issue + 15 days. Track B
   assumes 45 (the s.15 maximum). Which should A write, or should due_date be nullable?
   Do stated document_terms above 45 days count as an agreement for the MSMED cap? (VERIFY)
6. **TReDS pass-through vs discount.** CPSE settlement through TReDS routes the buyer's own
   payment via the platform; a discount is the financier paying early. Both credits come
   from the platform. Only the discount should be payer_kind treds_financier. How does the
   matcher tell them apart?
7. **TReDS fee and paid_date.** A financier pays gross minus TDS minus a discount fee. If the
   fee exceeds the settlement tolerance, allocated + TDS never reaches gross and the label
   never gets paid_date or settlement_kind. Should treds_discount settle the invoice
   regardless of the fee?
8. **as_of as a date.** Track B reads a date as the end of that day in UTC. If as_of dates are
   IST business days, the cutoff is 18:30 UTC. Which?
9. **SalesParam enum (additive).** The sales forecaster also produces `demand_ar_rho`,
   `demand_ar_sd`, `order_mean_boot_sd` and `purchase_ratio_boot_sd`. They are dropped when
   writing features.sales_process until the enum has them.
10. **forecast.shock_state (additive).** It holds z and z_sd at the last week only. Replaying a
    shock path also needs the AR(1) rho and q per sector; propose nullable `rho` and `q`
    columns.
11. **Calibrated bands (additive).** Propose `forecast.calibrated_bands` (tenant_id, run_id,
    horizon, target_date, level, lo_paise, hi_paise): the calibration component's band per
    horizon. `forecast.balance_quantiles` has fixed q05-q95 columns and cannot hold it.
12. **forecast.calibration_state (additive).** To resume calibration from the table alone it
    needs the unclipped alpha (alpha_t is a Prob, but ACI's level can go below 0) and somewhere
    for bands waiting for their outcome. Propose nullable `alpha_raw` and a
    `forecast.calibration_pending` table; until then the state lives in the runner.

## Ledger-complete-through watermark and live replay (2026-10-24)

**Watermark rule (proposed to Track A as the answer to question 1).** Per account, let L be
the largest reporting lag seen so far, max(known_at date - value_date) over the account's
known transactions. At a fetch at time T the ledger is taken as complete through
W = date(T) - L: every transaction value-dated on or before W is assumed to be known. With
several accounts, the tenant's W is the earliest. Each fetch re-stamps every open invoice
label with censored_at = W (known_at = T) and writes the bank-reported balance for date(T).
It needs no new column: Track B reads W back as the latest censored_at of open labels
(`io.adapters.ledger_complete_through`).

Track B cuts payment-timing labels at W and starts open-invoice survival at W
(survival_through_day = W - as_of). Labels must be cut at W, not as_of: open labels are
censored at W, so a later cut for paid ones counts recent payments without their unpaid
weeks. A test caught this (sector lateness residuals drifted to a mean of -0.12).
`timing_labels` says so in its docstring. The generator uses the same rule, so a direct cut
and a replay agree. On the default worlds L is 2 to 3 days, so W behaves like the old fixed
lag, but it is measured from data instead of hardcoded.

Not fixed by W: a payment value-dated before as_of but reported after it is in the
bank-reported balance while its invoice still looks open, so it is counted twice.

**Replay** (`synth/replay.py`). From one world, as_of steps forward a day at a time. Each
step reveals the contract rows with known_at in (previous as_of, as_of], including the
fetch's re-stamps, and appends them to the running tables. It also emits `ops.events` rows
(new_credit, new_invoice), so the real event path can replace the loop. Then it runs the
normal adapters and `run_tenant`. Each day's forecast is kept and scored against what
happened. `--paths demo` is 2,000 paths, about 3 s per step; `benchmark` is 10,000. Tests:
no step's tables hold a row known after its as_of; rerunning a day, or the whole replay,
is identical; the last step equals a direct run on `contract_tables_at(truth, as_of)`. Snapshot
IDs now hash the as-of view the components read, so two table histories that read the same
give the same run record.

**First learning-curve run: one world, one future, 30 daily steps from 1 Apr 2026, 2,000
paths.** n = 30 (step, date) pairs per horizon, but the steps share one realised path and
overlap, so this is closer to one observation than 30. It is a demo of the benchmark, not a
result.

| horizon (days) | n | mean error (Rs) | MAE (Rs) | flat-baseline MAE (Rs) | 50% band coverage | 90% band coverage |
|---|---|---|---|---|---|---|
| 1 | 30 | +2,26,579 | 3,40,705 | 2,89,355 | 0.23 | 0.87 |
| 7 | 30 | +2,44,984 | 7,29,947 | 8,92,669 | 0.53 | 0.77 |
| 14 | 30 | -1,37,022 | 5,49,513 | 10,53,074 | 0.80 | 0.97 |
| 30 | 30 | -10,71,677 | 11,80,292 | 25,48,646 | 0.50 | 0.93 |
| 60 | 30 | -1,12,495 | 11,24,811 | 27,50,077 | 0.53 | 1.00 |
| 90 | 30 | +34,04,811 | 34,04,811 | 9,94,955 | 0.00 | 0.50 |

The model beats carrying the balance forward at 7 to 60 days and loses at 1 and 90 days on
this path. Tracked buyer (most invoices, true weeks to pay once due 6.6): the 90% range of
1/p moves from 4.6-7.1 to 5.0-7.3 weeks as paid labels grow from 74 to 78, and contains the
truth throughout. The real benchmark needs many worlds and futures at 10,000 paths, with
steps scored independently.

## Shock correction on the current pipeline (2026-10-25)

Re-run of the base-logit shock correction (`shock_base`) on the current pipeline: contract
adapters, bank-reported opening balance, watermark-cut labels. Same 100 borderline pasts,
oracle 200 futures each, 4,000 paths.

| shock_base | mean gap | mean \|gap\| | corr | 0.05-0.20 pred / oracle | 0.20-0.40 | median gap day 30 / 90 (Rs) | receipts gap day 30 / 90 (Rs) |
|---|---|---|---|---|---|---|---|
| on (default) | -0.033 | 0.133 | 0.770 | 0.132 / 0.199 | 0.297 / 0.349 | -24,649 / -84,165 | +13,794 / -27,213 |
| off | -0.068 | 0.142 | 0.763 | 0.122 / 0.235 | 0.302 / 0.373 | +1,45,063 / +2,34,257 | +1,85,148 / +2,99,170 |

**Default stays on.** On this pipeline it is less wrong on every summary: mean |gap| 0.133 vs
0.142, mean gap -0.033 vs -0.068, and receipts within Rs 0.3 lakh at day 90 instead of
Rs 3.0 lakh over. The overshoot measured on 24 Oct came from the old 3-day label cut and the
reconstructed opening balance, not from the correction alone. The low bins are still
under-predicted by about 0.03 to 0.07.

**Stretch item: shock offset in the payment-timing likelihood.** The principled fix is still
to fit p with past z as a logit offset, so the posterior is a base chance by construction.
Caveat: the z estimates come from residuals against posteriors fitted without shocks, so
the fit has to alternate (fit timing with the current z offsets, recompute residuals and z,
refit, until stable) or fit both jointly. Worked through before coding; the base-logit
correction stays as the default until then.

## First real-data checks (2026-10-25)

Benchmarks only: nothing fitted here sets a production parameter. Datasets, licences and the
owner's decision to use them are in `eval/data/SOURCES.md`; full tables in `eval/results/`
(`results.parquet` plus one markdown file per check). Data enters through contract-shaped
loaders (`eval/models/loaders.py`) and the normal adapters.

**Kaggle data limits.** The 9,681 `isOpen` rows are a label-stripped test split, not unpaid
invoices. The labelled rows are right-truncated at the fiscal year end (an invoice appears only
if it cleared within its year), so there are no unpaid invoices and never-paid calibration
cannot be measured. At the horizons used the truncation removes little: of invoices issued
Jan-Mar 2019, 1.1% took more than 83 days and 0.45% more than 112.

**Payment-timing model** (train: issued before 2019-07-01, n = 17,644, open ones censored there;
test: issued Jul-Aug 2019, n = 5,955, followed 112 days; baseline due date):

| model | C-index (95% CI) | integrated Brier, weeks 1-15 | P(paid within 4w) pred / obs | ECE 4w |
|---|---|---|---|---|
| payment-timing model | 0.735 (0.727-0.743) | 0.031 | 0.874 / 0.903 | 0.033 |
| due date | 0.601 (0.593-0.611) | 0.052 | 0.914 / 0.903 | 0.050 |
| Cox | 0.770 (0.763-0.777) | 0.039 | 0.881 / 0.903 | 0.064 |
| random survival forest | 0.790 (0.781-0.797) | 0.028 | 0.903 / 0.903 | 0.005 |
| LightGBM person-period | 0.754 (0.744-0.764) | 0.026 | 0.906 / 0.903 | 0.005 |

It clearly beats the due date but loses to every feature-based model, which use the
customer's own payment history, amount and terms as features while it uses only the buyer
and due week. It under-predicts early payment (weekly geometric hazard; these buyers mostly
pay within days of the due date). The forest's settings were chosen on an internal
temporal split inside the training period, not on the test set.

**Sales forecaster weekly counts** (rolling one-week-ahead, 30 origins per tenant): negative
binomial beats Poisson on log score overall (-4.41 vs -6.54, n = 90) and per customer
(-2.02 vs -2.11, n = 3,364). 80% interval coverage: overall 0.97 (NB, too wide) vs 0.51
(Poisson); per customer 0.86 vs 0.82.

**Synthetic vs real.** A gradient-boosted classifier separates generator invoices from Kaggle
invoices almost perfectly (AUC 0.999-1.000, n = 28,087), even on per-invoice features only.
What the generator gets wrong, by feature: no part payments in Kaggle (generator 10%); 10
buyers per tenant against about 600; terms mostly 15 days (generator spread over 15/30/45);
payment close to the due date (median days late 0 vs 10); weekly counts far steadier relative
to their mean, partly because Kaggle tenants post about 680 invoices a week against 8.
Amounts relative to the tenant median match (univariate AUC 0.51). These are inputs for a
later generator preset, not parameters.

**Berka outside check** (n = 682 loans, 76 bad; only transactions before each loan date;
engine at 10,000 paths with the obligations detector and the unscheduled bootstrap, since the
flow forecaster is not built):

| score | AUC (95% CI) | minus best balance-only baseline (95% CI) |
|---|---|---|
| engine P(shortfall by day 90), threshold 0 | 0.639 (0.558-0.716) | -0.170 (-0.249 to -0.087) |
| engine, threshold = one monthly loan payment | 0.652 (0.568-0.730) | -0.156 (-0.237 to -0.074) |
| engine P(shortfall by day 30) | 0.658 (0.580-0.733) | -0.150 (-0.230 to -0.068) |
| minus 90-day minimum balance | 0.800 (0.733-0.859) | -0.008 (-0.038 to 0.026) |
| balance logistic regression, 5-fold CV (best baseline) | 0.808 (0.742-0.870) | |

The engine loses to balance-only scores. The unscheduled bootstrap draws each day
independently, so simulated balances drift like a random walk, while real balances revert
(income and spending are linked). This is the bar the flow forecaster has to clear. Lead
time is not measurable (only final loan status is given).

## Calibration component (2026-10-25)

`intelligence/.../calibrate/aci.py`. Adaptive conformal inference per tenant and per horizon
(1, 7, 14, 30, 60, 90 days), target coverage 0.80 (alpha = 0.20):
alpha_{t+1} = alpha_t + gamma (alpha - err_t). The band is read off the engine's own path
distribution at the [a/2, 1 - a/2] quantiles with a = clip(alpha_t, 0, 1); alpha_t itself is
kept unclipped (a band at a <= 0 is the whole line). Delayed feedback: a horizon-h band issued
on day t waits until the balance of day t + h is known (as-of read of ledger.balances) and only
then updates alpha. Option `method="online"`: the Gibbs & Candes (2022) step-size method, one
expert per gamma in the constants grid with exponential weights on the pinball loss (eta, sigma
from the paper with T = 365, an assumption in the constants file). The replay carries a
`Calibrator` from step to step: score bands whose outcome became known, run with the current
state, issue the new bands, write `forecast.calibration_state`.

**Benchmark (report only: gamma is not chosen on synthetic data for production).** Replays
from 2025-10-01 to each world's end (about 300 daily steps, 2,000 paths). The engine's paths
do not depend on calibration, so every method is replayed offline on the same paths (the
offline online-method replay reproduces the live run exactly, checked). The "off" column is the
same engine runs without calibration, i.e. the before numbers. The earlier 30-day learning-curve
replay (one world, 90% band) is not comparable and is not repeated here.

Coverage of the 80% band, three borderline worlds, targets from 2026-01-01, n = 630 per horizon
(210 per world; consecutive outcomes overlap and are strongly correlated, so the effective n is
far smaller):

| horizon (days) | off (before) | ACI 0.005 | ACI 0.01 | ACI 0.02 | ACI 0.05 | ACI 0.1 | online | split conformal |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.786 | 0.792 | 0.790 | 0.794 | 0.795 | 0.797 | 0.787 | 0.789 |
| 7 | 0.733 | 0.752 | 0.768 | 0.779 | 0.790 | 0.795 | 0.752 | 0.702 |
| 14 | 0.716 | 0.741 | 0.754 | 0.775 | 0.778 | 0.784 | 0.738 | 0.687 |
| 30 | 0.757 | 0.717 | 0.711 | 0.735 | 0.767 | 0.783 | 0.656 | 0.673 |
| 60 | 0.817 | 0.760 | 0.717 | 0.671 | 0.665 | 0.624 | 0.617 | 0.817 |
| 90 | 0.803 | 0.778 | 0.757 | 0.725 | 0.575 | 0.517 | 0.649 | 0.803 |

Short horizons (7, 14 days): the engine under-covers and ACI brings it to 0.78-0.80, more with
larger gamma. Long horizons (60, 90): the engine's own band is already right and ACI makes it
worse, more with larger gamma. With 60-90 days of delay and overlapping targets, misses arrive
in long runs about events that are already over, and ACI keeps reacting to them. The online
method sits in between at short horizons and is poor at long ones.

Shock: one world (past seed 0), every buyer's weekly payment chances x 0.3 from 2026-02-02;
n = 178 scored outcomes per horizon after the shock (7 per week). Weeks to recover = weeks
after the shock until trailing 4-week coverage is back at >= 0.75 after falling below it:

| horizon | off | ACI 0.005 | ACI 0.05 | online | split conformal (fitted before the shock, fixed) |
|---|---|---|---|---|---|
| 7 days | 4 | 4 | 4 | 4 | 4 |
| 14 days | never | 5 | 5 | 5 | never |
| 30 days | never | 17 | 10 | 15 | never |

At 14 days the uncalibrated band and split conformal stay at zero coverage for most of March to
June; ACI at gamma 0.05 holds at about 1.0 from March. One world and 7 outcomes a week, so the
recovery times are illustrative, not estimates.

**What this suggests (not a production choice):** calibrate short horizons only, or use a
horizon-dependent step (larger gamma short, little or none at 60-90 days), and treat ACI's
long-run guarantee as weak at these sample sizes. To decide on the Berka shock replay or on
real tenants, not here.

Contract gaps (questions 11 and 12 below): the calibrated band has nowhere to go
(`forecast.balance_quantiles` has fixed q05-q95 columns and the target is an 80% band), and
`forecast.calibration_state` cannot carry the state needed to resume (unclipped alpha_t, online
weights, bands waiting for their outcome). The replay carries the `Calibrator` object instead.

## Calibration horizons: ACI at 1-30 days only (2026-10-25)

**Decision.** The calibration component adapts horizons 1, 7, 14 and 30 days. At 60 and 90
days the band is the engine's own (level fixed at alpha = 0.20, never updated). Those bands are
still issued and scored, so their miss rate is reported in `forecast.calibration_state`.
Constant: `calibration.adapt_max_horizon_days` = 30. The calibration benchmark passes 90 to keep
comparing every method at every horizon.

**Why: delayed feedback.** A horizon-h band issued on day t is scored on day t + h, when that
day's balance is known. At 60-90 days, ACI learns of a miss two to three months after it issued
the band. Bands issued on consecutive days target overlapping futures, so one bad stretch shows
up as a run of 60-90 misses, all about events that are already over. ACI steps alpha down for
each one, then narrows again through a run of hits, so the level swings with past events. In
the benchmark above the engine's own 80% band covered 0.817 at 60 days and 0.803 at 90 (n = 630
each, effective n far smaller). Every ACI step size made that worse (0.517 to 0.778 at 90 days),
and so did the online method. At 7 and 14 days, where feedback arrives within two weeks, ACI
raises coverage from 0.73 / 0.72 to 0.78-0.80 at gamma 0.05. At 30 days it is roughly neutral.

What ships, read from the same benchmark (no re-run: the 60/90-day result is the "off" column):
ACI at 1-30 days, engine band at 60-90. gamma is still the paper value (0.005); choosing it is
left for the Berka shock replay or real tenants, as before.

## Requests to Track A

- Add 0% to the matcher's TDS candidate rates (below-threshold invoices carry no TDS).
- CPSE payments may arrive from a TReDS financier, not the buyer (CPSE settlement through TReDS
  is mandatory since June 2026).
- Use the Income-tax Act 2025 sections from 1 Apr 2026: 393(1) table entries for TDS,
  37(2)(g) for the old 43B(h).
- Tally/CSV is the main invoice path for micro suppliers below the Rs 5 crore e-invoicing
  threshold.
- Schedule III ageing layouts vary: some add an "Unbilled" column, some omit "Not due".
- State that `ledger.transactions.amount_paise` is signed (+ credit, - debit).
- Add `known_at` to `ledger.balances` so as-of balance queries are look-ahead safe.
- Expose the AA-reported account balance at fetch time (with its timestamp) alongside
  `ledger.balances`. Summing known transactions misses ones reported after as_of; on
  synthetic worlds that leaves the opening balance off by Rs 3.1 lakh (sd).

## Learned-mode calibration: diagnosis (2026-10-24)

Setup: 100 borderline pasts, oracle P(short by day 90) from 200 resampled futures per
past, learned engine at 4,000 paths. Tables are "binned by prediction": pasts grouped by
the engine's p_short(90), then the mean oracle p_short in each group. A calibrated
forecaster sits on the diagonal. No recalibration fitted on synthetic data is used.

**The pattern.** Low predictions are too low and middle ones too high (shipped settings:
predicted 0.12 vs oracle 0.21 in the 0.05-0.20 bin, 0.48 vs 0.41 in 0.40-0.60). That is
the signature of forecasts that vary more across tenants than the truth does: each
forecast carries per-tenant error the model does not represent, so extreme predictions
regress toward the middle. It is not a mean bias (mean gap about -0.015).

| variant (all four engine fixes on) | mean \|gap\| | corr | bin 0.05-0.20 pred / oracle | bin 0.40-0.60 |
|---|---|---|---|---|
| shipped: placeholder priors + group shrinkage, iid-week bootstrap | 0.143 | 0.729 | 0.121 / 0.207 | 0.482 / 0.400 |
| placeholder priors, no shrinkage | 0.143 | 0.728 | 0.121 / 0.197 | 0.484 / 0.411 |
| true type hyperpriors, no shrinkage | 0.139 | 0.747 | 0.126 / 0.203 | 0.491 / 0.390 |
| shipped + 4-week block bootstrap | 0.143 | 0.733 | 0.121 / 0.205 | 0.482 / 0.413 |
| block bootstrap + true priors, no shrinkage | 0.138 | 0.749 | 0.127 / 0.204 | 0.482 / 0.379 |
| diagnostic: true opening balance | 0.135 | 0.766 | 0.129 / 0.218 | 0.479 / 0.464 |
| diagnostic: true opening + true priors | 0.132 | 0.778 | 0.127 / 0.199 | 0.478 / 0.476 |

**What moved it.**
- Priors (step 1): little. True hyperpriors improve mean |gap| by 0.004 and correlation by
  0.02; the low bin does not move. Placeholder priors are not the main cause.
- Block bootstrap (step 2): almost nothing on the table; kept because it is the correct
  bootstrap under autocorrelation (purchase-ratio and order-rate spreads widen slightly).
- Opening balance (extra diagnostic): the largest single effect. The observed balance at
  as_of misses transactions reported after as_of (known_at lag): on these worlds it is on
  average Rs 1.64 lakh below the truth (sd Rs 3.1 lakh, range -12.0 to +4.8 lakh). Using the
  true balance fixes the middle bins and removes the steady -Rs 1 lakh median bias (it
  becomes about +Rs 0.5 lakh), but leaves the low bin and makes the mean gap -0.047.

**What is left.**
1. Shock double counting (leading hypothesis). The payment-timing posterior p is already
   the marginal weekly chance, averaged over past sector shocks. The engine then adds
   logit-scale shock noise on top, which for small p raises the average weekly chance
   (Jensen). Evidence: the receipts gap went from about -Rs 0.4 lakh to +Rs 0.7 lakh at day
   30 when sector shocks were switched on, and stays positive in every variant. Fix to
   try: solve for a base logit so that E[sigmoid(base + shift)] over the shock
   distribution equals the posterior p.
2. Opening balance. A product issue, not a model one: the engine must start from the
   bank-reported balance at as_of (the AA account summary carries the current balance at
   fetch time), not from summing known transactions. Request to Track A: expose the
   AA-reported balance with its timestamp alongside `ledger.balances`.
3. Components without posterior uncertainty: invoice amounts resampled from history,
   unscheduled flows bootstrapped, a single shock loading of 1 per sector (the generator
   plants per-buyer loadings around -1, sd 0.3).
4. Whatever remains after 1-3 is the calibration component's job (adaptive conformal on
   real outcomes), not a fit to synthetic data.

## Learned-mode calibration: shock sign and double counting (2026-10-24)

**Sign check (fixed by test, no code change needed).** The lateness residual is
z = -(sum(y - h)) / sum h(1 - h), so more payments than expected give negative z, and the
engine applies shift = -z to logit p. A positive lateness shock lowers the weekly chance.
Test: hold every sector's z at +3 in learned mode; receipts by day 30 fall by more than 20%
for both open and future invoices, and the median balance is never higher.

**Double counting (fixed as specified).** Each posterior draw p (and p_early) is replaced, for
future weeks only, by a base chance sigmoid(b) with E_z[sigmoid(b - z)] = p under the
sector's long-run shock distribution z ~ N(0, q / (1 - rho^2)) (Newton on 32-point
Gauss-Hermite). The filtered current z is then applied on top. Past weeks (survival, the
elapsed part of the current week) keep the posterior. Sectors without a shock fit are
unchanged. Tests: the quadrature identity and a 200,000-path Monte Carlo through the engine
hook. Note on the requested test wording: with z held at its long-run mean (0) the chance
is sigmoid(b), not p; it is the average over the long-run z distribution that equals p,
which is what the test checks.

**Re-run (100 borderline pasts, oracle 200 futures, 4,000 paths, same cache as above).**

| variant | mean gap | mean \|gap\| | corr | 0.05-0.20 pred / oracle | 0.40-0.60 | receipts gap day 30 / 90 (Rs) |
|---|---|---|---|---|---|---|
| before: shocks on top of the posterior | -0.016 | 0.143 | 0.733 | 0.121 / 0.205 | 0.482 / 0.413 | +72,823 / +1,35,374 |
| after: base logit | +0.021 | 0.143 | 0.738 | 0.136 / 0.193 | 0.494 / 0.356 | -97,101 / -1,91,601 |
| after + true opening (diagnostic) | -0.012 | 0.132 | 0.771 | 0.129 / 0.188 | 0.486 / 0.393 | same as above |

Mean |gap| does not move. The low bin improves (gap 0.084 to 0.057); the middle bin gets
worse (0.069 to 0.138). Receipts flip from over-forecast to a larger under-forecast.

**Why it overshoots (hypothesis, not yet tested).** The target E_z[h] = p assumes p is the
plain average of the weekly chance over shocks. It is not: a constant-hazard fit gives
p roughly equal to payments / at-risk weeks, and with persistent shocks (rho about 0.8)
invoices spend more at-risk weeks in slow periods, so p already sits below the
shock-averaged chance. Lowering it further double-corrects. The consistent fix is to put
the past z estimates into the timing likelihood as a logit offset (estimate b directly from
labels with known offsets), so the posterior is a base chance by construction. That
changes the payment-timing model's Gibbs step and should be worked through before coding.
`shock_base=False` restores the previous behaviour for comparison.

**What remains.**
1. The shock/timing interaction above (offset in the likelihood).
2. Opening balance (product issue, request to Track A already listed).
3. Components without parameter uncertainty: invoice amounts resampled from history,
   unscheduled flows bootstrapped from history, and equal shock sensitivity (loading 1)
   for every buyer in a sector, where the generator plants per-buyer loadings around 1
   in magnitude with sd 0.3.
4. Whatever remains after these is left to the calibration component (adaptive conformal
   on real outcomes), not fitted to synthetic data.
