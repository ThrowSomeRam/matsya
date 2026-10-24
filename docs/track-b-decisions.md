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
