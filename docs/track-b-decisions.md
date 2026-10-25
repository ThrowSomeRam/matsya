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
$\theta$ the shortfall threshold (`shortfall_threshold_paise`): the owner cushion, default 0,
never negative. Overdraft headroom is not part of it. The decision engine offers overdraft
only as explicit draws with their interest cost, so the limit is counted once.

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
13. **Scenario transforms in forecast.runs (additive).** `overrides_json` is validated as a list
    of `events.OverrideItem`, which only has `one_off` (kind, on, amount_paise; extra fields
    forbidden). The scenario engine's transforms (rate_multiplier, date_shift, cash_event with a
    window, terms_change, freeze) do not fit. Propose new `OverrideKind` values and
    `OverrideItem` as a tagged union matching `scenarios.transforms`. Until then a scenario
    run's `forecast.runs` row carries `scenario_id`, and the full list (canonical JSON and its
    sha256) is in `forecast.scenarios.params_json` and the run record.
14. **forecast.scenario_effects (additive).** `forecast.scenarios` holds one row per scenario
    (params_json, breaking_magnitude). The per-day comparison has nowhere to go. Propose
    `forecast.scenario_effects` (tenant_id, scenario_id, base_run_id, run_id, date, day,
    p_short_base, p_short, d_p_short, d_es_cond_paise, d_es_uncond_paise). The same numbers can
    be derived from `forecast.shortfall` for the two run_ids; the scenario row's params_json
    also carries runway_lost_days.

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

**In the production path (2026-10-25).** `run.py` now calibrates horizons 1, 7, 14 and 30 days
(`calibrate.PRODUCTION_HORIZONS`).
- **Each run:** it first scores the bands from earlier runs whose day's balance is now known
  (`adapters.known_balances`, an as-of read of `ledger.balances`), then issues this run's bands
  from the engine's paths, and writes `forecast.calibration_state`.
- **State:** level, pending bands and recent outcomes are kept as JSON per tenant under
  `<out>/calibration/`. JSON floats round-trip exactly, so reloading gives the same run as
  keeping the object (tested over five daily runs).
- **Still open:** the calibrated band has no contract table (question 11), so it is only in the
  state file and the run's return value.

## Payment-timing model: due-week phase tried and dropped (2026-10-25)

**Variant.** Three weekly chances instead of two: p_early before the due week (k < d), p_due in
the week holding the due date (k = d), p after it (k > d), each Beta and still conjugate, since
every week is one Bernoulli trial on exactly one chance. Paid in week k: early failures
min(k-1, d-1), due-week failures [k > d], late failures max(0, k-d-1); open after c weeks:
min(c, d-1), [c >= d], max(0, c-d). The never-paid Gibbs step only gains a (1 - p_due) factor
in the survival. Rule set in advance: keep it only if it beats the two-phase model on the
Kaggle benchmark.

**Kaggle result** (same split as above, n = 5,955 test invoices, baseline due date; rows in
`eval/results/results.parquet` at commit 27d37a1, model `payment_timing_model_due_week`):

| model | C-index (95% CI) | integrated Brier, weeks 1-15 | 4w pred / obs, ECE | 8w | 12w |
|---|---|---|---|---|---|
| two-phase (current) | 0.735 (0.726-0.744) | 0.0313 | 0.874 / 0.903, 0.033 | 0.949 / 0.961, 0.014 | 0.978 / 0.989, 0.013 |
| three-phase (due week) | 0.721 (0.714-0.731) | 0.0319 | 0.857 / 0.903, 0.049 | 0.937 / 0.961, 0.024 | 0.972 / 0.989, 0.018 |
| Cox | 0.770 (0.762-0.778) | 0.039 | 0.881 / 0.903, 0.064 | 0.947 / 0.961, 0.016 | 0.969 / 0.989, 0.023 |

**Dropped:** worse on every metric. Not a bug: mean weekly mass by week since issue
(observed / two-phase / three-phase) is week 2 0.258 / 0.136 / 0.125, week 3 (the due week for
most invoices, 15-day terms) 0.594 / 0.524 / 0.557, week 4 0.037 / 0.087 / 0.050. The new phase
fits the due week and the week after better, but the two-phase model's excess in week 4 had
been offsetting its shortfall in week 2, so removing it lowers P(paid within 4 weeks) further
below observed and the ranking gets worse. The real misfit is before the due date: a constant
p_early cannot put a quarter of payments in the week just before the due week and almost none
in week 1. Code removed; the result rows stay.

Note: the bootstrap seeds for each model's C-index CI come from one shared stream, so adding a
model shifted the later models' CIs by about 0.001; the random survival forest's C-index moved
0.790 to 0.789 between runs (not bit-identical).

## Scenario engine (2026-10-25)

`intelligence/.../scenarios/`. One primitive: a typed list of transforms (pydantic, tagged on
`type`) applied at run time to a copy of the simulation engine's inputs, never to stored tables
or fitted components.

| transform | what it changes | magnitude |
|---|---|---|
| rate_multiplier | weekly payment chance x factor for buyers, sectors or all buyers (weeks starting after as_of only; survival to the cut keeps the posterior); or the order rate x factor | factor |
| date_shift | fixed obligations by kind (salary, rent, emi, ...) or obligation_id, or every future supplier payment, by N days (earliest day 1). GST is refused: statutory date, and its amount depends on the month | days |
| cash_event | signed amount on a date, or on a day drawn per path uniformly in [on, window_end] | amount_paise |
| terms_change | buyer terms -> to_days: the due week of open and future invoices for future weeks; supplier terms from -> to: every supplier payment moves by to - from days | to_days |
| freeze | a fraction of each path's balance on the day before start is unusable for N days (out and back as two flows, so balances still add up from flows) | fraction |

Transforms compose: multipliers multiply, shifts add, the last buyer terms win, cash events and
freezes accumulate. The engine's old `OneOffEvent` stays (it is what `forecast.runs`
accepts); `cash_event` is its generalisation.

**Order rate.** The sales forecaster draws its base orders exactly as before, then thins them
(k < 1, each order kept with chance k) or adds Poisson((k - 1) lambda) orders a week (k > 1)
from a separate generator. Purchases are generated per order, so they follow at the purchase
ratio and are a subset of the baseline's on every path; fixed obligations are untouched.

**Common random numbers.** `run_engine(..., seed_id=base_run_id)`: a scenario run has its own
run_id (`<base>:scenario:<scenario_id>`) but every stream derives from the baseline's. The
scenario's own randomness (window days, extra orders) uses a separate `engine:scenario`
stream, so baseline streams are unchanged. Exact consequences, tested: a one-off moves every
path by exactly its amount from its date; the "unexpected expense" preset changes only the
scenario cash flow and "revised payment schedule" only fixed obligations. Not exact: when a
scenario changes which invoices exist (fewer orders), the later draws in that stream line
up differently, so the difference also carries some noise. Payment-chance changes are exact
since the decision engine (2026-10-25): every row draws a uniform every week.

**Overrides never update learned parameters.** Tested: after running every preset, the inputs'
posterior draws, open invoices, fixed obligations and features pickle identically, and a refit
gives byte-identical payment timing, obligations and sales features.

**Outputs.** Per day: d_p_short, change in conditional and unconditional expected shortfall;
runway lost = baseline runway - scenario runway, with a runway beyond the horizon counted as
H + 1 (so it is a lower bound then, flagged). Written: `forecast.scenarios` (params_json = full
transform list, its hash, base and scenario run_ids, runway lost, breaking-point grid) and the
scenario run's `forecast.runs` / `balance_quantiles` / `shortfall`. The per-day frame waits for
a table (question 14).

**Presets** (`intelligence/config/scenario_presets.yaml`, dates as `+Nd` after as_of; magnitudes
are placeholders): delayed customer payments (all buyers x 0.5), unexpected expense (Rs 2 lakh
out in 14 days), reduced sales (orders x 0.7), supplier tightens terms (30 -> 15 days), revised
payment schedule (EMI 7 days earlier). Owner edits are the same list, built ad hoc.

**Breaking point.** For one transform in a list (others held), a grid of runs on the same seed
from a benign magnitude to a severe one (defaults per type: factor 1 -> 0, fraction 0 -> 1, cash
0 -> 10x the amount, buyer terms from -> 4x to, supplier terms from -> 0, shifts 0 -> 4x days).
The grid checks that p_short(day) rises with the magnitude and brackets the first crossing of
the owner's target; bisection narrows it to tol. Drops on the grid are reported, not hidden.
Tested on a closed form: for an outflow A on day D, p_short(90) is the share of baseline paths
that breach before D or whose minimum from D on is below |A|; bisection lands within tolerance.

**Default world (past seed 0, 2,000 paths, target 0.5 at day 90; baseline p_short(90) 0.141,
runway beyond 90 days).** One world, so illustrative only:

| preset | d_p_short day 30 / 90 | runway lost (days) | breaking magnitude | monotone |
|---|---|---|---|---|
| delayed customer payments | +0.320 / +0.729 | >= 60 | factor 0.70 | yes |
| unexpected expense | +0.016 / +0.032 | >= 0 | Rs 15.8 lakh on day 14 | yes |
| reduced sales | -0.035 / -0.114 | >= 0 | none: p_short falls | no |
| supplier tightens terms | +0.492 / +0.497 | >= 71 | 18 days (from 30) | yes |
| revised payment schedule | +0.004 / +0.018 | >= 0 | not reached by 28 days earlier | yes |

**Reduced sales lowers shortfall risk within 90 days here.** Purchases are paid about the
purchase lag before each invoice and the invoice is paid weeks after it, so over 90 days new
orders cost cash before they bring it in; fewer orders means less working capital tied up.
The model is doing what it should; for this world the brief's "reduced sales" stress only
shows beyond the horizon, or with fixed costs that scale. The breaking-point search reports it
as not monotone (p_short falls along the whole grid) rather than inventing a number.

## Scenario engine: empirical buyers and sticky purchases (2026-10-25)

**Empirical timing in the engine.** The merge of the per-buyer empirical mode left the simulation
engine on the Beta-geometric chances for every buyer (only the payment-timing model's own
`features.invoice_timing` used the hazards). `TimingDraws.hazard` now carries each empirical
buyer's weekly hazard (None for the rest); in every week the engine takes h at bin k - due week
in place of the two-phase chance. The never-paid draw is unchanged (pi and the Beta-geometric
survival to the cut, as in `TimingModel.predict_invoice`). On that hazard, in order: the sector
shock as a logit shift (base hazards solved with `base_chance` like the two-phase chances, so the
shock-averaged hazard is the fitted one), then the scenario multiplier k, h x k clipped to [0, 1],
for weeks starting after as_of. New buyer terms move the bin (k - new due week) for future weeks.
`predict_invoice` applies the shock shift to the hazard on the logit too. Tested: dropping the
hazards changes the paths; x 0.5 on the empirical buyers cuts receipts; with every buyer empirical,
x 0 leaves no future-invoice receipts. Model version `beta-geometric-2phase-v1+empirical-v1`.

**Sticky purchases under reduced sales.** Thinning orders used to cut their purchases at once.
But supplies for orders already placed are still paid: the purchase cut now lags the sales cut
by the fitted purchase lag L plus `purchase_stickiness_days` (on `rate_multiplier` with target
orders; default 14, assumption). A dropped order's purchase stays if it falls on day <= L + S, so
purchases match the baseline exactly on days 1..L+S (tested). Added orders (k > 1) keep their
purchases as before. Preset `reduced_sales` now carries `view_days: [90, 180]`; it should be run
on inputs built at a 180-day horizon (obligations and GST dates beyond 90 days).

`eval/models/scenario_views.py` (`eval/results/scenario_views.md`): default world, 20 pasts x
2,000 paths, orders x 0.7, baseline the same world and seed. Means over pasts, ES in Rs lakh:

| variant | day | p_short base / scenario | d p_short (sd over pasts) | ES uncond base / scenario | ES cond base / scenario |
|---|---|---|---|---|---|
| stickiness 14 d (preset) | 90 | 0.284 / 0.234 | -0.050 (0.038) | 3.72 / 2.58 | 11.16 / 9.39 |
| stickiness 14 d (preset) | 180 | 0.398 / 0.412 | +0.014 (0.058) | 6.26 / 6.41 | 13.20 / 12.69 |
| stickiness 0 d | 90 | 0.284 / 0.170 | -0.114 (0.063) | 3.72 / 1.64 | 11.16 / 8.25 |
| stickiness 0 d | 180 | 0.398 / 0.343 | -0.055 (0.058) | 6.26 / 4.87 | 13.20 / 11.47 |

Stickiness halves the 90-day "benefit" of fewer orders; at 180 days the preset is about neutral
(+0.014, se about 0.013 over 20 pasts). So in this world reduced sales still does not show as a
stress: purchases and invoice cash fall together, and the cash freed from purchases offsets the
lost receipts for most of the half year. A stress would need fixed costs that do not scale, a longer
stickiness, or margin compression; those are owner edits, not defaults.

## Payment-timing model: per-buyer empirical timing kept (2026-10-25)

**Mode.** For buyers with >= 8 paid bills, timing comes from their own history by week relative
to the due week, j = k - d (j = 0 holds the due date), as a discrete weekly hazard: e_j payments
and r_j invoices at risk in bin j, each invoice entering at j = 1 - d and censored open invoices
counted at risk up to c - d. Each bin is shrunk Beta-Binomial style toward the group's pooled
hazard g_j = (E_j + 0.5) / (R_j + 1): h_j = (e_j + s g_j) / (r_j + s), s = prior_strength (8).
(Superseded: the shrink target, the end bins and the bins beyond the data changed; see
"Empirical timing in the engine: late bins".)
A hazard rather than a histogram so censoring and the bins before issue are handled exactly. The
never-paid share is unchanged: open invoices count at risk with weight 1 - P(dead | open) from the
Beta-geometric posterior, and predictions use its P(dead). Buyers with fewer bills, and unseen
buyers, keep the Beta-geometric model. Code: `timing/empirical.py`, `fit_timing(empirical=True)`
is now the default. Rule set in advance: keep it only if it beats the current model on Kaggle.

**Kaggle result** (same split, n = 5,955 test invoices, baseline due date, rows in
`eval/results/results.parquet`, model `payment_timing_model_empirical`):

| model | C-index (95% CI) | integrated Brier, weeks 1-15 | 4w pred / obs, ECE | 8w | 12w |
|---|---|---|---|---|---|
| Beta-geometric only | 0.735 (0.727-0.744) | 0.0313 | 0.874 / 0.903, 0.033 | 0.949 / 0.961, 0.014 | 0.978 / 0.989, 0.013 |
| with empirical mode | 0.743 (0.731-0.751) | 0.0274 | 0.878 / 0.903, 0.026 | 0.950 / 0.961, 0.011 | 0.981 / 0.989, 0.010 |
| Cox | 0.770 (0.763-0.777) | 0.0386 | 0.881 / 0.903, 0.064 | 0.947 / 0.961, 0.016 | 0.969 / 0.989, 0.023 |

Share paid by week since issue, predicted / observed (weeks 1-6): Beta-geometric 0.127 / 0.015,
0.136 / 0.258, 0.524 / 0.594, 0.087 / 0.037, 0.038 / 0.024, 0.017 / 0.016; empirical 0.027 / 0.015,
0.259 / 0.258, 0.541 / 0.594, 0.052 / 0.037, 0.031 / 0.024, 0.018 / 0.016; Cox 0.014 / 0.015,
0.304 / 0.258, 0.515 / 0.594, 0.048 / 0.037, 0.029 / 0.024, 0.020 / 0.016.

**Kept:** better than the current model on every metric, and fixes the pre-due misfit recorded
above (week 1 and week 2). Better than Cox on Brier and calibration, still behind it on ranking
(C-index 0.743 vs 0.770). Still under-predicts the due week (0.541 vs 0.594). Open points: s is the config value, not tuned; the day-of-month add-on for within-week placement is
not built (the weekly Kaggle metrics cannot score it).

## Decision engine (2026-10-25)

`intelligence/.../decide/`. Each action is a scenario transform list with an exact cost in paise
(constants `decision.*`), applied on the baseline's seed.

| action | transforms | cost in the objective |
|---|---|---|
| TReDS discount (per open invoice of a CPSE buyer, or one the owner marks onboarded; not yet due after the lag) | `invoice_cash` replace: A - A r d / 365 on day L, the invoice leaves payment timing | A r d / 365, d = days from day L to the due date |
| chase a buyer | `rate_multiplier` buyer x 1.5 for 2 weeks (new `weeks` field) | Rs 500 |
| MSMED claim (micro/small tenant, invoice past its s.15 deadline) | `invoice_cash` if_unpaid: principal + interest on day 90, only on paths still unpaid then | penalty (10% of principal) - recovery (0.5) x P(unpaid by day 90) x interest x (1 - tax rate): the recovered interest is taxable income |
| overdraft draw | `cash_event` + k x limit / 10 on day d in {1, 15, 30, 45, 60, 75} | A r (H + 1 - d) / 365 (repaid after the horizon) |
| stretch a supplier | `date_shift` supplier_payments, share s (new field), N in {7, 14, 30} days | owner penalty; micro/small paid past its s.15 deadline (written terms capped at 45 days, else 15; VERIFY): MSMED interest on the expected stretched amount for the late days the stretch adds (full cost: s.23 makes it non-deductible), plus the delay in the s.43B(h) / 37(2)(g) deduction: tax rate x amount net of GST x cost of funds x FYs moved (0 for a late payment in the same FY) |

Rates without a source are config defaults with status verify: TReDS 9%, overdraft 11%, claim
cash lag 90 days (s.18(5) award window). The income-tax rate is an owner input by entity type
(proprietor, partnership, company); only the company rate has a default (25.17%, verify).
Without a rate, a micro/small supplier stretch or an open MSMED claim is refused. The cost of
funds for the deduction delay is an owner input, defaulting to the overdraft rate. Worked
example (tested): as_of 1 Mar 2026, half of Rs 1 lakh a day, 30-day terms stretched 30 days:
Rs 30 lakh of payments move one FY, cost 0.2517 x 30 lakh x 10% = Rs 75,510 with no GST (the
earlier full-tax rule gave Rs 7,55,100), Rs 63,991.53 with 18% GST netted off. The shortfall threshold is the owner cushion and is never
negative (decide refuses one); the overdraft limit enters only as draws with their interest,
so it is counted once (tested). Interest is
P (prod_k (1 + 3 r_k / 12) - 1) with r_k the bank rate in force on each monthly rest date; an
incomplete last month accrues pro rata by days (VERIFY both). The bank-rate entry dated TODO is
read as in force from the earliest date. The issue date stands in for acceptance. Suppliers are
an owner input (id, share of supplier payments, MSME class, terms, penalty, tightened): the
engine's purchases carry no supplier. A supplier is never stretched if the owner flags it as
tightened or the active scenario has a supplier terms_change (which covers every supplier).

**Optimiser.** Sample-average chance constraint on a 1,000-path subsample (seed
`<run>:decide:subsample`): binary actions, integer overdraft units, a binary z_n per path,
B[n, t] + sum_v delta_v[n, t] x_v + M[n, t] z_n >= theta for t <= 90, sum z <= floor(eps N),
at most one action per invoice, one stretch length per supplier, units <= the limit. A chased
invoice that is also discounted or claimed loses its chase effect: y = x_chase (1 - x_invoice),
linearised. Rows that hold for every plan are dropped; M is the tightest valid big-M; a path no
plan can save gets z = 1. HiGHS via `scipy.optimize.milp`. If eps cannot be reached, a first
solve minimises sum z and a second finds the cheapest plan at that share; the result says so.
eps defaults to 0.05 (status decide).

**Deltas.** TReDS, claim and overdraft effects are exact from the baseline's per-invoice payment
days (tested against engine runs, equal to the paisa). Chase and stretch effects come from
scenario runs on the same seed. Engine change to make that exact: payment simulation now draws a
uniform for every row every week, active or not, so a change to one buyer's chances moves only
that buyer's payments (tested). Same distribution as before, different draws: earlier runs'
numbers are not reproduced bit for bit at this commit. Remaining non-additivity (GST on stretched
purchases; a claim on a chased invoice) is covered by the fresh-path check.

**Engine stream change check.** The run record now carries `engine_version` (`mc-v2`, also in
`model_versions` as `simulation_engine`); v1 is the stream before this change. Same scripts and
seeds at 43b54b5 (v1) and here (v2); error bars are Monte Carlo standard errors of the
difference (independent streams) and a paired bootstrap over pasts (5,000 resamples).

| check | n | v1 | v2 | v2 - v1 | se | z |
|---|---|---|---|---|---|---|
| true-parameter p_short, 3 curated pasts x days 30/60/90 | 10,000 paths, oracle 200 futures | | | +0.0004 to +0.0094 | 0.002-0.007 | max +1.69 |
| learned mean gap p_short(90) | 100 pasts, 4,000 paths | -0.1490 | -0.1489 | +0.0001 | 0.0007 | +0.21 |
| learned mean \|gap\| | same | 0.1799 | 0.1792 | -0.0007 | 0.0007 | -1.08 |
| learned corr | same | 0.736 | 0.739 | +0.003 | 0.003 | +1.07 |
| breaking point, delayed payments (factor) | default world, 10,000 paths, target 0.5 | 0.612 | 0.612 | 0 | 0.003 | 0 |
| breaking point, supplier terms (days) | same | 15 | 15 | 0 | 0.17 | 0 |

Per past, |z| > 2 in 2 of 100 learned pasts (4.5 expected by chance). Unexpected expense,
reduced sales and revised schedule reach no breaking point at either version. Nothing moves
beyond its error: the stream change alters draws, not distributions. All nine true-parameter
differences are positive; they share paths within a past (three independent pasts), and the
pooled day-90 z is +1.3.

Note: the learned-mode figures here (mean gap -0.149, |gap| 0.180) were off the recorded
-0.033 / 0.133 at both versions. Cause and fix: "Empirical timing in the engine: late bins"
below.

**Verification and outputs.** The chosen transforms run on 10,000 fresh paths (seed
`<run>:decide:verify`) against the same-seed baseline. `decisions.plans`: rank by leave-one-out
importance on the subsample (p_short rise when the action is dropped), then cost;
`p_short_after` is cumulative on the fresh paths, adding the actions in rank order, and the last
row is the full plan's run. The plans table holds max(cost, 0) (claims can have negative net cost);
the decision result's plan frame keeps the signed figure in `net_cost_paise`, dropped from the
contract write until question 16 is answered.
The run record keeps both seed ids, the active and plan transform lists (canonical JSON and hash),
eps, day and threshold; a rerun gives an identical plan, record and baselines (tested). The amount
owed under MSMED on every overdue invoice is reported whether or not a claim is chosen.

**Default world** (past seed 0, threshold Rs 20 lakh so the world is risky, small tenant, overdraft
limit Rs 10 lakh, suppliers S1 micro 50% and S2 other 30%, both 30-day terms; 1,000 subsample
paths, 10,000 fresh paths; eps 0.05 at day 90). One world, illustrative only:

| baseline | actions | cost (Rs) | p_short(90) | expected days below threshold |
|---|---|---|---|---|
| do nothing | 0 | 0 | 0.707 | 9.56 |
| discount every eligible invoice | 7 | 2,442 | 0.630 | 6.40 |
| chase the largest buyer | 1 | 500 | 0.684 | 8.49 |
| plan | 8 | 22,743 | 0.045 (in sample 0.050) | 0.38 |

Plan (company tenant, s.15 deadline = written terms): Rs 7 lakh overdraft on day 1 (Rs 18,986
interest), S2 +30 days (Rs 2,000; not MSME), two chases (Rs 500 each) and four TReDS discounts
(Rs 757 together). S1 is not stretched: with 30-day written terms any stretch is late and owes
interest. No claim is chosen: net of tax, the expected interest no longer beats the penalty.
83 candidates, 10,504 constraint rows after pruning, about 88 s, of which HiGHS about 47 s.
Before the s.15, GST and claim-tax changes the same world gave Rs 13,705 net (Rs 14,849
clamped), p_short 0.036, with S1 +14 days and four claims of negative net cost.

**Contract gaps (proposals, additive).** 15. `decisions.plans` has no amount or parameters: an
overdraft row's amount is in target_id (`day1:40000000`), a stretch's days in target_id
(`S1:+14d`). Propose nullable `amount_paise` and `params_json` (the action's transforms). 16. A
negative net cost (claims) cannot be stored (cost_paise is non-negative); propose nullable
`net_cost_paise`. 17. Nowhere for the baselines comparison, the MSMED amount owed or the
decision run record (seeds, transform lists); propose `decisions.baselines`,
`decisions.msmed_owed` and a `decisions.runs` row.

## Decision engine across worlds and claim sensitivity (2026-10-25)

**Across 30 worlds** (`eval/models/decision_baselines.py`, `eval/results/decision_baselines.md`):
past seeds 0-9 of the healthy, borderline and distressed presets; small company tenant,
overdraft limit Rs 10 lakh, S1 micro 50% and S2 not MSME 30% on 30-day written terms; eps 0.05
at day 90; 1,000 solve and 10,000 fresh paths per world. Each world's opening balance is lowered
by the median do-nothing 90-day minimum from a pilot run on its own seed (2,000 paths), with the
threshold left at 0. That is the same arithmetic as a threshold at that median and keeps the
threshold a cushion. Realised do-nothing p_short 0.482-0.531, 30/30 inside 0.2-0.8.

| strategy | worlds | net cost mean (sd), Rs | median | p_short(90) mean (sd) | days below |
|---|---|---|---|---|---|
| do nothing | 30 | 0 | 0 | 0.501 (0.012) | 8.75 |
| discount every eligible invoice | 21 | 7,222 (5,892) | 2,903 | 0.387 (0.083) | 5.79 |
| chase the largest buyer | 30 | 500 | 500 | 0.470 (0.021) | 7.92 |
| plan | 30 | 54,431 (45,193) | 27,059 | 0.054 (0.005) | 0.50 |

The plan reaches the target in 30/30 worlds within Monte Carlo error (fresh p_short <= eps +
2 x 0.0091) but only 5/30 strictly: fresh-path p_short averages 0.054 against 0.049 in sample.
This is the usual optimism of a sample-average chance constraint: the solver pushes the
in-sample count to the cap, so fresh paths land a little above it. No world was unreachable and
no baseline matched the plan's p_short for less. Net and clamped costs are equal in every world:
no plan chose a claim. Solve time median 94 s, max 129 s. Plan cost rises with the preset's
margin pressure (healthy median about Rs 14k, distressed about Rs 1 lakh).

**Follow-ups (same 30 worlds, same seeds).** Three changes. (1) The MILP solves at
0.8 x eps in sample (`decision.solve_eps_factor`) and the plan is still judged at eps on fresh
paths. (2) Solver status and MIP gap go into the run record (`solver_status`, `time_limited`,
`mip_gap`, `eps_solve`) and the eval. (3) MSMED claims are no longer plan actions: their cash
lands on day 90 and cannot lower p_short. `DecisionResult.money_owed` lists each overdue
invoice with the amount owed and the net value of claiming (value after tax and recovery minus
the relationship penalty), best first.

| | solve at eps | solve at 0.8 eps |
|---|---|---|
| plan meets eps strictly on fresh paths | 5/30 | 24/30 |
| within Monte Carlo error | 30/30 | 30/30 |
| fresh p_short mean (sd) | 0.054 (0.005) | 0.046 (0.005) |
| plan cost mean / median (Rs) | 54,431 / 27,059 | 61,899 / 50,819 |
| stopped at the 60 s time limit | not recorded | 5/30 (max MIP gap 0.87) |

The tighter in-sample target costs about Rs 7,500 a plan on average. Money owed: 1,453
overdue invoices across the worlds, none worth claiming at the default recovery and penalty.
Open: five plans stop at the time limit, one far from proven optimal.

**Chase columns: trim tested and rejected.** Variant: chase only the 3 buyers with the most open
cash, and drop the chase x invoice interaction variables (`decision.chase.max_buyers` = 3,
`decision.chase.interaction` = false). Same 30 worlds and seeds
(`eval/results/decision_baselines_chase-interaction-False_chase-max_buyers-3_worlds.json`):

| | default | trimmed |
|---|---|---|
| strict target hits | 24/30 | 24/30 |
| mean fresh p_short | 0.0455 | 0.0449 |
| mean plan cost (Rs) | 61,899 | 65,520 |
| stopped at the time limit | 5/30 | 4/30 |
| median solve time | 86 s | 76 s |
| median candidates | 26 | 19.5 |

Paired cost difference +Rs 3,621 (95% CI +975 to +7,303); dearer in 21 of 30 worlds. The rule
was to trim only if cost and strict hits were unchanged within noise; cost is not, so the
defaults stay (every buyer with open cash, interaction on). The two settings remain in the
constants file for later runs.

**Claim sensitivity** (`eval/models/decision_sensitivity.py`, `decide(constants_overrides=...)`;
default world, past seed 0, Rs 20 lakh cushion, same seeds in every run):

| run | claims chosen | net cost (Rs) | vs default | fresh p_short |
|---|---|---|---|---|
| default (recovery 0.5, penalty 0.10) | 0 | 28,334 | 0 | 0.0561 |
| claims off | 0 | 28,334 | 0 | 0.0569 |
| recovery 0.25 | 0 | 28,977 | +644 | 0.0546 |
| recovery 0.75 | 6 | 9,043 | -19,291 | 0.0560 |
| penalty 0.05 | 13 | -25,883 | -54,217 | 0.0568 |
| penalty 0.20 | 0 | 28,334 | 0 | 0.0561 |

Two findings. (1) Claims are chosen only for their expected net value, never for risk: their
cash lands on day 90, so they leave p_short unchanged and the rest of the plan stays the same
(non-claim cost Rs 28,334 in every run with claims). Mixing them into the plan's cost hides that;
reporting claims separately from the risk plan would be clearer (proposal, not done).
(2) The solver stops at its 60 s time limit on this world (HiGHS status 13 for both the default
and the recovery 0.25 runs, checked on a rerun), so these plans are the best found, not proven
optimal. That explains recovery 0.25 costing Rs 644 more for an identical risk problem (claims
there are never worth taking) and claims off swapping one chase for another at the same cost:
the claim columns change the search path. Plan identity and, at the time limit, cost are not
stable across candidate lists. The 30-world run did not record solver status; 60 s is about
the median HiGHS time, so some of those plans are likely time-limited too. Options: record the
status and MIP gap in the run record, raise the limit, or tighten the formulation (fewer chase
columns, symmetric overdraft days). Not done.

## Decision engine: CVaR plan (2026-10-25)

**Formulation** (`decide/plan.py` `_solve_cvar`, `decide(method="cvar")`). Rockafellar-Uryasev on the
same 1,000 solve paths as the chance-constrained plan. The loss per path is the 90-day shortfall depth,
L_n = max(0, theta - min_t balance).

- **Objective:** minimise the total action cost.
- **Path rows:** u_n + eta + sum_v delta_v[n, t] x_v >= theta - B[n, t] for every kept (n, t).
- **CVaR row:** eta + sum_n u_n / (alpha N) <= budget.
- **Bounds:** u >= 0 and eta >= 0. Loss is never negative, so eta >= 0 loses nothing.
- **Other rows:** the same as the chance-constrained plan (one action per invoice, one stretch per
  supplier, the overdraft limit, the chase-invoice linearisation).

There are no per-path binaries. The only integers are the actions (about 26 candidates, median), so the
problem is still a MILP, but a small one.

Owner inputs: `decision.cvar.alpha` (0.05) and `decision.cvar.budget_paise` (0). With budget 0 the plan
must leave no solve path short, so alpha has no effect. Where some paths cannot be saved by any action,
the budget is infeasible. A first solve then minimises the CVaR, and a second finds the cheapest plan
within 0.1% (or Re 1) of that minimum. The result says so.

Same flow as before:

- verified on 10,000 fresh paths, with the same seeds as the chance-constrained plan (common random
  numbers);
- the plan table and LOO ranking are unchanged, with in-sample CVaR as the tie-break;
- solver status and time are recorded.

The chance-constrained plan stays available (`method="chance"`). The 30-world baseline, sensitivity
scripts and their tests pin it.

**Benchmark** (`eval/models/decision_cvar.py`, `eval/results/decision_cvar.md`). Same 30 worlds and
owner inputs as "Decision engine across worlds". Means over worlds, fresh paths:

| plan | cost mean (median), Rs | p_short(90) | meets 0.05 strictly | within MC error | ES if short, Rs | ES over all futures, Rs | worst-5% average shortfall, Rs | solve s median (max) | optimal / time limit |
|---|---|---|---|---|---|---|---|---|---|
| do nothing | 0 | 0.498 | 0/30 | 0/30 | 11,19,050 | 5,58,867 | 31,67,241 | | |
| chance-constrained | 54,106 (26,209) | 0.0467 | 19/30 | 26/30 | 6,40,485 | 30,701 | 6,12,092 | 31.1 (60.1) | 26 / 4 |
| CVaR | 1,15,801 (1,21,524) | 0.0075 | 30/30 | 30/30 | 5,40,676 | 4,811 | 96,224 | 1.4 (4.2) | 30 / 0 |

- **Cost:** CVaR is dearer in every world (paired +Rs 61,695, 95% CI +49,676 to +73,543). It buys
  about a sixth of the tail shortfall.
- **Budget in sample:** met in 7/30 worlds. The other 23 have solve paths no action can save, and get
  the lowest-CVaR plan.
- **Solver:** HiGHS proves every CVaR plan optimal in under 5 s. 4/30 chance-constrained plans stop at
  the 60 s limit.
- **Chance-constrained, strict hits:** 19/30, down from 24/30 in the earlier run on the same worlds.
  That run was before the sales bootstrap came back (wider sales paths on the fresh check).

**Stress** (pessimistic model: every payment chance x 0.85 and the order rate x 0.9, as scenario
transforms on the fresh seed; plans not re-solved):

| plan | p_short(90) normal -> stressed | meets 0.05 stressed | worst-5% normal -> stressed, Rs |
|---|---|---|---|
| do nothing | 0.498 -> 0.639 (+0.141) | 0/30 | 31,67,241 -> 37,82,850 (+6,15,609) |
| chance-constrained | 0.0467 -> 0.1136 (+0.0670) | 0/30 | 6,12,092 -> 13,53,151 (+7,41,059) |
| CVaR | 0.0075 -> 0.0280 (+0.0205) | 27/30 | 96,224 -> 3,78,575 (+2,82,351) |

The chance-constrained plan sits at the edge of its target, so a mild model error pushes every world
over it. Its tail also grows more than doing nothing's does. The CVaR plan keeps its margin.

**Default.** The rule set in advance: CVaR becomes the owner-view default if it meets the owner's target
on fresh paths at least as often. It does (30/30 against 19/30), so `decision.plan_method` = `cvar`.
The cost of that choice is roughly double the plan cost. If the owner's target is the 5% chance and
nothing more, the chance-constrained plan is the cheaper answer and still available.

## Empirical timing in the engine: late bins (2026-10-25)

**What moved.** Learned-mode calibration on the 100 default pasts (oracle 200 futures, 4,000
paths, `engine.evaluate._learned_one`) reproduced the recorded -0.033 / 0.133 / 0.770 exactly at
ef5a591, c4acc4f, 96dfccf and 1e4ba02, and gave -0.149 / 0.180 / 0.736 from 6e07b3d, the merge
that put the empirical hazards into the engine. The script was right. With the hazards switched
off, today's code gave -0.033 / 0.135 / 0.767 and the receipts gap fell from +Rs 7.7 lakh to
+Rs 14k at day 30; no other flow changed.

**Cause: two bugs in the late bins** (j = weeks past the due week), both hitting long-overdue bills.
1. The end bin j = 26 holds every later week, but each invoice added one week of exposure there
   however long it stayed. An invoice paid in week 41 was one event over one week. Hazards of
   0.48-0.58 a week came out where the Beta-geometric p was 0.08-0.16, and the engine used them
   for every week past 26.
2. Bins with no exposure fell back to the group prior (E + 0.5) / (R + 1) = 0.5 a week.
On past 0, invoices 4+ weeks past due (Rs 63.9 lakh) paid Rs 11.9 lakh by day 30 with the
hazards, Rs 6.9 lakh without. The Kaggle check scores weeks 1-15 and could not see it.

**Fix.** The end bins count invoice-weeks, so their hazard is a weekly rate (tested: a constant
0.2 beyond j = 26 comes back within 0.005 on 20,000 invoices). Shrinkage is to the buyer's own
fitted Beta-geometric chance, m_j = p_early before the due week and p from it on (posterior
means; the buyer's fit is already shrunk to its group), replacing the pooled group hazard:
h_j = (e_j + s m_j) / (r_j + s). Empirical bins are used up to the last bin with at least
`payment_timing.empirical_min_exposure` (5, assumption) invoice-weeks at risk; beyond it the
parametric chance applies. Predictions (features, Kaggle) use its posterior mean there; the
engine gets NaN bins and uses each posterior draw's own p, which keeps parameter uncertainty
(with the mean instead, receipts by day 30 were +Rs 87k and the gap -0.050). Open invoices still
count at risk weighted by 1 - P(dead | open).

| variant (100 pasts, 4,000 paths) | mean gap | mean \|gap\| | corr | median gap day 30 / 90 (Rs) | receipts gap day 30 / 90 (Rs) |
|---|---|---|---|---|---|
| recorded reference (ef5a591, no empirical hazards in the engine) | -0.033 | 0.133 | 0.770 | -24,649 / -84,165 | n/a |
| before the fix (6e07b3d to here) | -0.149 | 0.180 | 0.736 | +7,34,620 / +9,59,790 | +7,72,077 / +10,16,694 |
| fix, posterior mean beyond the data | -0.050 | 0.143 | 0.745 | +48,110 / +42,037 | +87,329 / +1,04,573 |
| fix, per-draw p beyond the data (kept) | -0.045 | 0.142 | 0.746 | +21,747 / +2,954 | +60,213 / +64,149 |

Calibration table (kept variant): predicted / oracle 0.031 / 0.067 (n 9), 0.126 / 0.194 (27),
0.299 / 0.374 (30), 0.481 / 0.496 (15), 0.706 / 0.713 (17), 0.863 / 0.745 (2). The low and middle
bins are still under-predicted, as before. Receipts remain about Rs 0.6-0.9 lakh over the oracle;
the empirical hazards inside the data are point estimates and carry no parameter uncertainty.

**Guard.** `test_learned_mode_calibration_has_not_moved` (slow) fails if mean gap or mean |gap|
moves more than 0.02 from the reference; `MATSYA_ORACLE_CACHE` reuses the oracle pickle.

**Kaggle re-run after the fix** (same split, n = 5,955 test invoices): empirical C-index 0.741
(0.733-0.748), integrated Brier 0.027, ECE at 4 / 8 / 12 weeks 0.028 / 0.011 / 0.009; Beta-geometric
0.735, 0.031, 0.033 / 0.014 / 0.013. Still better on every metric, so the mode stays (before the
fix: 0.743, 0.027, 0.026 / 0.011 / 0.010; the change is within the C-index interval).

## Flow forecaster: unscheduled flows (2026-10-25)

`intelligence/.../flows/`, wired into the engine as `build_learned_inputs(flow_forecaster="quantile")`.

**Decision (2026-10-25): the same-weekday bootstrap stays the default; the quantile flow forecaster
stays in the code as an option, off by default.** Reasons, from the results below:
- On the 30- and 90-day totals of unscheduled flow it ties the bootstrap (CRPS 1.02x and 0.93x,
  intervals across 1).
- On the 90-day shortfall check (Berka outside check) it is worse: AUC 0.587 against 0.639,
  paired -0.051 (CI -0.088 to -0.014).
- Cause: it under-counts occasional large flows. Salaries, pensions and deposits land on a few
  days a month, and five quantiles cannot carry their mass.

Revisit with the two-part model proposed at the end of this section.

**Scope.** Unscheduled flows only (bank charges, refunds, misc income and expense, cash
withdrawals, owner transfers). Invoices and purchases belong to the payment-timing model and
the sales forecaster, GST to the sales forecaster, fixed obligations to the obligations detector.

**Model.** Per category, linear quantile regression (q 0.05/0.25/0.5/0.75/0.95, HiGHS, Rs
thousand) on weekday, day-of-month band, month-end (last 3 days), an Indian festival window (the
day and the 7 before; Diwali, Holi, Eid al-Fitr, Dussehra, Christmas, 2022-2027, constants,
VERIFY) and a mean-reverting term: (yesterday's balance - usual) / spread, usual = median
closing balance over the 180 days before the day (fixed at as_of in the forecast), spread = the
interquartile range over the 180 days to as_of, floored at Rs 1 lakh. A 90-day window chased the
balance: in a recovery the balance sits above its lagging median while flows are still
positive, and the fit got the sign wrong (tested). Crossing quantiles are sorted per day.

**Persistence.** An AR(1) Gaussian copula per category; rho is the lag-1 correlation of the
normal scores of the history's PIT values. Sparse flows sit on a repeated quantile (zero) most
days; such days take a PIT drawn within the tied range (randomised PIT, fixed seed). A mid-point
made quiet days look persistent (owner transfers rho 0.39 on a world that draws them
independently); with the randomised PIT the generator's independent flows come out at rho about 0.

**Engine.** Unscheduled flows are simulated last, day by day, after every other flow, so each
path's own balance feeds the deviation term. A consequence for the decision engine: an action's
cash is partly offset later by the reverting flows, so its linear deltas are no longer exact
(within 1% of the action's cash on the default world, tested); the fresh-path run is the check.

**Short history.** Under 8 weeks: Chronos-2 zero-shot quantiles per category (optional extra
`chronos`, CPU torch; amazon/chronos-2; no balance term), else the bootstrap. Tested on a weekly
spike, which it places correctly.

**Berka, unscheduled flows** (`eval/models/flows_benchmark.py`, n = 300 accounts with >= 1 year of
history, origin 90 days before the last transaction, 1,000 sample paths; Kaggle copy of PKDD'99):

| metric | flow forecaster | bootstrap (8 weeks) | same weekday last week |
|---|---|---|---|
| CRPS, daily | 792 (0.61) | 879 (0.68) | 1,296 |
| pinball, daily | 368 (0.57) | 438 (0.68) | 648 |
| CRPS, 30-day sum | 6,223 (0.37) | 6,101 (0.36) | 16,843 |
| CRPS, 90-day sum | 12,635 (0.25) | 13,587 (0.27) | 50,369 |

(ratio to same weekday last week in brackets). Against same weekday last week it wins on every
metric with the paired interval clear of 0. Against the bootstrap: daily CRPS 0.90x (paired CI
-97 to -77, better in 96% of accounts), pinball 0.84x; the 30- and 90-day sums are level (1.02x
and 0.93x, intervals across 0). The gain is in day-to-day shape, not yet in the totals that
move the balance.

**Learned-mode calibration (guard).** 100 pasts: -0.041 / 0.143 / 0.746 (bootstrap: -0.045 /
0.142 / 0.746; reference -0.033 / 0.133). Unscheduled mean gap at day 90: -Rs 20k (bootstrap
+Rs 21k). Guard passes.

**Berka outside check** (`eval/models/berka_checks.py`, 682 loans, 76 bad, 10,000 paths, both flow
models on the same seeds, Kaggle copy). The bootstrap engine and every balance-only baseline
reproduce the recorded AUCs to three decimals on this copy (engine 0.639 / 0.652 / 0.658, best
baseline 0.808), so its small differences do not move this check.

| score | AUC (95% CI) | minus balance logistic 0.808 |
|---|---|---|
| engine, flow forecaster, p_short(90) thr 0 | 0.587 (0.509-0.662) | -0.221 |
| engine, flow forecaster, p_short(90) thr payment | 0.609 (0.529-0.688) | -0.199 |
| engine, flow forecaster, p_short(30) thr 0 | 0.653 (0.579-0.722) | -0.155 |
| engine, bootstrap, p_short(90) thr 0 | 0.639 (0.558-0.716) | -0.170 |
| engine, bootstrap, p_short(30) thr 0 | 0.658 (0.580-0.733) | -0.150 |

Target (beat 0.808) not met, and the forecaster is worse than the bootstrap at 90 days: paired
AUC difference -0.051 (CI -0.088 to -0.014); at 30 days -0.005 (CI -0.033 to 0.024). It raises
good accounts' p_short(90) (0.286 to 0.330) and leaves bad ones' (0.430 to 0.438).

**Cause: lumpy inflows.** On 60 benchmark accounts the forecaster's mean 90-day misc_income
(salaries, pensions and deposits arrive as transfers in) is Rs 29,120 against Rs 42,009 at the
historical rate and Rs 48,055 realised; outflow categories are close. Five quantiles cannot carry
the mass of a flow that happens on a few days a month: the mean of the interpolated quantile
function falls well below the true mean. Net 90-day sum: forecaster Rs 1,930, bootstrap
Rs 10,989, realised Rs 14,163. Salaried (mostly good) accounts drift down and look riskier.

**Proposed fix (not done).** A two-part model per category: P(non-zero day | features) by
logistic regression, and quantile regression of the amount on non-zero days only; sampling
draws the occurrence, then the amount. That keeps a lumpy flow's mean and is the standard form
for intermittent series. Recurring income (a salary on a fixed day) may also belong with the
obligations detector's recurrence detection, which today looks at outflows only.

## Attribution (2026-10-25)

`intelligence/.../attribute/drivers.py`, simple version: neutralise one driver at a time on the
same seed (common random numbers) and measure the risk that goes away.
- **Invoice:** the open invoice is paid in full on its due date (day 1 if already overdue). Only
  that invoice's cash changes on each path, so its effect is computed exactly from the
  baseline's payment days, with no re-run.
- **Buyer:** its weekly payment chance is scaled to its group's typical chance (the median
  posterior-mean p of the buyers in its sector) by a rate multiplier, and the engine is re-run on
  the baseline's seed. The never-paid share is unchanged.
- **Contribution:** baseline minus neutralised, for p_short and for the unconditional expected
  shortfall (mean depth below the threshold), at days 30, 60 and 90.
- **Output:** the top 10 by 90-day p_short go to `decisions.drivers` (`delta_p_short` = the
  90-day value). The 30/60-day and expected-shortfall columns are in the result only.
  - *Contract proposal (additive):* nullable `horizon_days` and `delta_es_paise` on
    `decisions.drivers`.
- **Called on demand**, like the decision engine; not in `run.py`.

Default world (past seed 0, Rs 20 lakh cushion, 10,000 paths): 113 drivers in 62 s (about 20 s at
4,000 paths). The top 10 are all overdue invoices; the largest would cut p_short(90) by 0.50 if
paid on day 1.

Tests:

| test | result |
|---|---|
| driver with no effect (an invoice worth 0) | contributes exactly 0 |
| planted late buyer (largest buyer at a tenth of its weekly chance throughout) | ranks first among buyers |
| same inputs and seed | identical table |
| top 5 across 3 seeds (slow test) | see below |

The top 5 are stable within Monte Carlo error, not exactly. Ranks 1-4 are identical across three
seeds at 4,000 paths. Ranks 5 and 6 swap: their contributions differ by 0.001-0.003, against a
standard error of the difference of about 0.008. The test therefore checks that any driver moving
in or out of the top 5 is within 2 standard errors of the 5th-place value.

**Finding: a slowdown that starts mid-history is missed.** The planted buyer slowing to a tenth
of its chance six months before as_of was not ranked first among buyers. The payment-timing model
pools the whole history, so it estimated p 0.169 where the post-change truth is 0.015. Attribution
ranks what the model believes, so it misses what the model misses. The overdue invoices that pile
up do show as invoice drivers. Change-point detection (BOCPD in the handbook, not built) is what
would catch the slowdown itself.

## Audit simplifications (2026-10-25)

From the audit in `docs/math-and-constants.md`.

**Categoriser: majority vote replaces the label model.** The rules' votes are combined by a plain
majority. Rule confidence is the share of fired functions that agree; it gates the rule step at
0.5 and weights the weak training labels. The classifier and the LLM fallback for abstains stay.
Template hold-out benchmark (synthetic, n = 3,134 per set):

| | before (label model) | after (majority vote) |
|---|---|---|
| macro-F1, in template | 0.997 | 0.997 |
| macro-F1, held out | 0.992 | 0.992 |
| abstain share, held out | 0.002 | 0.002 |
| own classifier alone, held out | 0.663 | 0.685 |

`categorise/label_model.py` is gone.

**Payment-timing group shrinkage: kept; thin-buyer test added**
(`eval/models/thin_buyers.py`, 30 worlds with 40 buyers). Thin = 1-3 paid bills, n = 252; heavy =
20 or more, n = 80. Error = |posterior mean - true value|, paired bootstrap over buyers.

| buyers | quantity | MAE, group shrinkage | MAE, type prior only | difference (95% CI) |
|---|---|---|---|---|
| thin | p (weekly chance once due) | 0.0851 | 0.0816 | +0.0035 (+0.0002 to +0.0069) |
| thin | p_early | 0.0348 | 0.0330 | +0.0018 (+0.0005 to +0.0032) |
| thin | expected weeks to pay, 1/p | 7.96 | 4.60 | +3.37 (+1.46 to +6.06) |
| heavy | p | 0.0538 | 0.0532 | +0.0006 (-0.0015 to +0.0029) |

Shrinkage is slightly worse for thin buyers, not better.
- **p and p_early:** both versions are unbiased on average (true mean p 0.222; shrunk 0.221,
  type only 0.220), so the cost is a little extra error.
- **Weeks to pay:** the large gap comes from the group priors' shape. At the strength cap of 8,
  a group with a low mean gets alpha below 1, which allows p near 0, and the posterior mean of
  1/p blows up. The type priors all have alpha above 1.
- **Proposal (not done):** floor the group prior's alpha at 1.5, or shrink only p and pi and not
  their spread. The placeholder type priors are not the generator's, so the comparison is
  "group prior vs placeholder", as in production.

**Calibration: online step-size method removed.** It was worse than plain ACI at every horizon
on the replay benchmark (above). Its last numbers stay in `results.parquet`; the benchmark now
checks the live replay against offline ACI at the paper gamma.

**Sales forecaster: one variability mechanism left (none).**
- **Removed first:** the persistent demand level (fitted, unused by default).
- **Ablation of the other two** on the learned-mode calibration guard (100 pasts, 4,000 paths,
  oracle 200 futures; guard = within 0.02 of the reference -0.033 / 0.133):

| demand AR(1) | parameter bootstrap | mean gap | mean \|gap\| | corr | guard |
|---|---|---|---|---|---|
| on | on (before) | -0.0450 | 0.1422 | 0.746 | pass |
| off | on | -0.0502 | 0.1473 | 0.739 | pass |
| on | off | -0.0459 | 0.1440 | 0.744 | pass |
| off | off (kept) | -0.0496 | 0.1484 | 0.738 | pass |

- **Kept: both off**, the simplest variant that passes, as the rule set in advance.
- **Cost:** it is the worst of the four on all three metrics, and its mean |gap| is 0.015 from
  the reference against a limit of 0.02, so little guard margin is left.
- **Contract:** `features.sales_process` loses the `demand_level_sigma` row. The enum member stays
  (removal proposed for the next gate).

**Decision engine: chase trim rejected** (see "Decision engine across worlds").

**Constants: one source for legal and statutory numbers.**
- **`matsya_intelligence/statutory.py`** reads them from the constants file; no literal copies
  remain. It covers:
  - s.15 deadline days (45 / 15);
  - the MSMED 3x multiplier and the covered classes;
  - the GST rate (new entry `gst.standard_rate`, 0.18, to verify);
  - QRMP days 22 / 24 (to verify);
  - the runway rule (`simulation.runway_p_short` 0.5).
- **Generator:** reads the CA-fee TDS rate and its GST default from the file too.
- **Removed (nothing read them):**
  - `simulation.n_paths / horizon_days / seed`;
  - `flows.quantiles / quantile_crossing_fix`;
  - the `shocks` block;
  - `decision.relationship_penalty`;
  - `treds.discount_rate`.
- **Moved:** the timing type priors into `payment_timing.type_default_priors` (status
  placeholder).
- **Kept:** sections 1-2 of the file (legal and dataset facts the handbook mirrors), whether or
  not code reads them.

## Thin-buyer floor and sales bootstrap restored (2026-10-25)

**Payment-timing model: group prior alpha floored at 1.5.** The proposal from the thin-buyer test,
done. The group's p prior keeps its method-of-moments mean, and its strength is raised where needed so
that alpha >= 1.5 (`payment_timing.group_prior_min_alpha`, `timing/hierarchy.py`). Only p is floored:
1/p is the quantity that blew up. Same 30 worlds, 40 buyers, seeds and bootstrap as before
(`eval/results/thin_buyers.md`):

| buyers | quantity | type prior only | shrinkage, before | shrinkage, alpha >= 1.5 | difference now (95% CI) |
|---|---|---|---|---|---|
| thin (n = 252) | p, MAE vs truth | 0.0816 | 0.0851 | 0.0847 | +0.0031 (-0.0003 to +0.0065) |
| thin | expected weeks to pay (1/p), MAE vs truth | 4.60 | 7.96 | 5.24 | +0.65 (+0.42 to +0.90) |
| thin | p_early | 0.0330 | 0.0348 | 0.0349 | +0.0019 (+0.0005 to +0.0033) |
| heavy (n = 80) | p | 0.0532 | 0.0538 | 0.0537 | +0.0005 (-0.0016 to +0.0028) |

- **What improved:** the weeks-to-pay error falls by 2.7 weeks.
- **What did not:** shrinkage is still worse than the type prior alone on weeks to pay. On p it is
  now a tie (the CI crosses 0).
- **Heavy buyers:** unaffected.
- **Decision:** shrinkage stays on, as before. The floor is kept because it removes the blow-up and costs
  nothing on p.

**Sales forecaster: parameter bootstrap restored; demand AR(1) stays removed.** Per-path block-bootstrap
draws of (m, k, purchase ratio) are back in `sales/model.py` (`sample(param_draws=True)`), and the engine
uses them by default (`build_learned_inputs(sales_uncertainty=True)`; model version `negbin-v1+boot`).
The two tests for them are back as well. Learned-mode calibration guard, with the alpha floor also in
(100 pasts, oracle 200 futures, 4,000 paths):

| variant | mean gap | mean \|gap\| | corr | guard (within 0.02 of -0.033 / 0.133) |
|---|---|---|---|---|
| both off (audit) | -0.0496 | 0.1484 | 0.738 | pass, margins 0.0034 / 0.0046 |
| bootstrap on, AR(1) off (now) | -0.0510 | 0.1472 | 0.739 | pass, margins 0.0020 / 0.0058 |

- **Better:** mean |gap| and corr, close to the audit's off/on run (-0.0502 / 0.1473 / 0.739).
- **Worse:** the mean-gap margin, now 0.0020 and the binding one. Any further change that pushes
  forecasts more optimistic will trip the guard.
- **Bins:** the 0.05-0.20 bin still under-predicts (0.126 predicted vs 0.214 oracle, n 28).

## Owner view payload (2026-10-25)

`present/owner_view.py`: `build_owner_view(tenant_run) -> OwnerView` (pydantic). `present/tenant_run.py`:
`compute_tenant_run` runs everything once.

- **Baseline:** the decision engine's fresh-path do-nothing run (`<run>:decide:verify`). The plan's
  "risk after" figures therefore share every random draw with the headline and chart 2.
- **What-ifs:** every preset, each with its breaking point, run against that baseline.
- **Drivers:** attribution, on its own seed.

**No LLM.** Strings come from fixed templates, and every number a string shows is also a payload field
(`Money.text`, `Pct.text` or an integer field). A test checks this, along with every rupee figure
against the engine, decision engine, scenario and attribution outputs.

**Money format:** Rs 4.2 lakh, Rs 1.3 crore, en-IN grouping below a lakh.

**Breaking point:** the magnitude at which the 90-day chance reaches 0.5, the runway rule.

**Fan:** the engine's own quantiles. Days 1-30 are flagged as calibrated, and would be read at the
calibration component's level. The demo world has no calibration history, so the scale is 1 and those
bands equal the engine's.

**Demo payload** (`eval/models/owner_view_demo.py`): `eval/demo/owner_view.json`, built in 620 s at
10,000 paths. Default world, past seed 0, Rs 10 lakh cushion, categoriser categories, CVaR plan.

- **Today:** Rs 28.6 lakh in the bank.
- **Risk:** 20% / 29% / 34% chance of running short within 30 / 60 / 90 days; runway beyond 90 days.
- **Plan:** 6 steps for Rs 1.2 lakh take the 90-day chance to under 1%. Solved optimal.
- **Money owed:** 54 overdue invoices carry Rs 6.9 lakh of MSMED interest.

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
