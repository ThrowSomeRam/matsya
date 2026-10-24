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
