# Track B: the maths and constants actually in the code

**Since this audit (same day, uncommitted batch).** Details in `docs/track-b-decisions.md`
("Audit simplifications", "Attribution").
- **Categoriser:** majority vote replaced the label model.
- **Calibration:** the online method was removed; calibration now runs in `run.py` for horizons
  1-30.
- **Sales forecaster:** the demand level, demand AR(1) and parameter bootstrap were removed.
- **Constants:**
  - legal and statutory numbers come from the constants file through `statutory.py`;
  - unread entries were removed;
  - the timing type priors moved into the file as placeholders.
- **Attribution** is built.
- **Kept after test:** chase columns (trim rejected) and group shrinkage (thin-buyer test shows
  it slightly worse).

The tables below describe the code at 38ec284.

Audit of `intelligence/` at commit 38ec284 (branch `track-b/decision-engine-checks`), read from
the code, not the handbook. Evidence comes from `docs/track-b-decisions.md` and
`eval/results/`. One new number: the categoriser benchmark was run for this audit (it prints
only; nothing was saved).

**Status words.**
- *Default*: implemented and run by `matsya_intelligence.run` (the production path).
- *On demand*: implemented and tested, but called by its own entry point, not by `run.py`.
- *Off*: implemented, behind a flag that defaults to off.
- *Stub*: a package with only a docstring.

**Constant status.** The constants file is `intelligence/config/track_b_constants.yaml`
("the file").
- *Verified*: checked against a cited source.
- *To verify*: stated from general knowledge; the file says so.
- *Assumption*: our choice, not measured.
- *Tuned*: set by an experiment, which is named.
- *Placeholder*: stands in until something else supplies it.
- **HC**: hard-coded outside the file.

Short glossary of terms used below:
- **Beta / Beta-geometric:** a Beta distribution describes an unknown probability.
  Beta-geometric means "pay this week with chance p, independently each week", with p itself
  uncertain.
- **Gibbs sampling:** draws from a posterior by updating one unknown at a time given the others.
- **Quantile regression:** fits a chosen percentile (say the 95th) of an outcome as a straight
  line in the features, instead of fitting the mean.
- **Kalman filter:** the standard recursive estimate of a hidden quantity that drifts over time,
  observed with noise.
- **MILP:** an optimisation over yes/no and whole-number decisions with linear constraints.
- **Metrics:**
  - AUC: chance a random bad case scores above a random good one (0.5 is a coin flip).
  - C-index: the same idea for payment timing.
  - CRPS: the error of a whole forecast distribution against the outcome (lower is better).
  - ECE: calibration error.
  - Macro-F1: classification accuracy averaged over classes.

---

## 1. Generator (synthetic worlds)

**Status:** default data source (`config/data_source.yaml`: `DATA_SOURCE: synth`, borderline
preset, past seed 0). Also the oracle for every learned-mode check.

**Maths.** `synth/world.py: generate_world`.
- **Buyers:** each draws a weekly payment chance p, an early chance p_early and a never-paid
  share pi from Beta priors for its type.
- **Payment week:** geometric. An invoice is paid in each week with chance p_early before the due
  week and p from it on. That chance is shifted on the log-odds scale by a sector lateness shock
  z, an AR(1) series: each week's value is rho times last week's plus noise.
- **Orders:** weekly counts are negative binomial (Poisson with gamma-distributed rate), with an
  AR(1) demand level. Amounts are lognormal.
- **Purchases:** a fixed share of each order, paid L days before the invoice is issued.
- **Fixed costs and unscheduled flows:** fixed costs on fixed days; unscheduled flows Poisson per
  day.
- **Seeds:** past and future draws come from separate seeds, so `resample_futures` gives exact
  oracle probabilities for one fixed past.

| constant | value | set in | status |
|---|---|---|---|
| type priors (p, pi, p_early) | cpse Beta(6,10)/(1,99), listed (4,12)/(2,98), unlisted (3,15)/(4,46); p_early (1,19) | `synth/config.py` DEFAULT_TYPE_PRIORS | assumption |
| sector shock rho, sigma, loading | 0.8, 0.3, mean -1 sd 0.3 | `synth/config.py` | assumption |
| orders per week, dispersion, demand rho / sigma | 8, 5, 0.8 / 0.1 | `synth/config.py` | assumption |
| invoice amount | median Rs 1.5 lakh, lognormal sigma 0.8; terms 15/30/45 | `synth/config.py` | assumption |
| purchase ratio | 0.47 (presets: healthy 0.29, distressed 0.66) | `synth/config.py` PRESETS | tuned on the oracle: median past p_short(90) of 0.03 / 0.28 / 0.82 |
| purchase lag | 10 days +- 3 | `synth/config.py` | assumption |
| fixed costs | salary Rs 12 lakh, rent 2 lakh, EMI 3 lakh, CA fee Rs 25k | `synth/config.py` | assumption |
| PF / ESI rates | 0.12 / 0.0325 of salary | `synth/config.py` | to verify (flagged VERIFY in code) |
| GST rate | 0.18 | `synth/config.py` | assumption (one rate for all goods) |
| part payment | 10% of invoices; first part 30-70%; rest 7-28 days later | config + **HC** `world.py` | assumption |
| unscheduled flows | misc credits 0.2/day (median Rs 5k), expenses 0.5/day (Rs 1.5k), cash withdrawals 0.5/week, owner transfers 1/month | `synth/config.py` | assumption |
| unscheduled amounts | lognormal sigma 0.6; refund vs misc income 50/50; cash Rs 10k-50k; owner transfer Rs 50k-2 lakh; bank charges Rs 59-590 monthly, Rs 15-59 quarterly | **HC** `world.py` | assumption |
| TDS on the CA fee | 10% | **HC** `world.py` (the file has `tds.rates.professional_fees` 0.10) | verified value, duplicated |
| statutory days (TDS 7th, PF/ESI 15th, GSTR-3B 20th, Sunday shift) | from the file | `statutory_calendar` | verified |
| reporting lag (known_at) | 0-2 days + 6-23 h | **HC** `world.py` | assumption |

**Evidence.** A classifier separates generator invoices from real Kaggle invoices with AUC
0.999-1.000 (n = 28,087). The main differences:
- no part payments in Kaggle;
- about 600 buyers per tenant against our 10;
- terms mostly 15 days;
- median days late 0 against our 10.

So the generator tests mechanics; its numbers are not realistic.

---

## 2. Categoriser

**Status:** default (`run.py` calls `Categoriser().fit().predict()`). The LLM step is a stub
that always abstains (`categorise/llm.py: llm_label`). Every engine benchmark uses the
generator's true categories instead (`engine/evaluate.py`), so the categoriser's errors never
reach a reported engine number.

**Maths.**
1. **Rules:** 21 labelling functions vote or abstain: keywords, plus structural rules on rail,
   company names, GSTINs, amount size, recurring same-amount debits and repeat counterparties
   (`categorise/lfs.py: LABELLING_FUNCTIONS`).
2. **Label model:** a one-accuracy-per-rule Dawid-Skene model combines the votes, fitted by EM
   with no gold labels. EM alternates between "which class is each row" and "how accurate is
   each rule" (`categorise/label_model.py: LabelModel`).
3. **Classifier:** character 2-5-gram TF-IDF logistic regression, trained on the label model's
   output (`categorise/classifier.py`).
4. **Cascade order** (`categorise/pipeline.py: Categoriser.predict`): owner override, then
   confident rules, then confident classifier, else abstain.

| constant | value | set in | status |
|---|---|---|---|
| rule threshold, classifier threshold | 0.5, 0.6 | **HC** `pipeline.py` | assumption |
| min weak-label confidence, owner-correction weight | 0.5, 5.0 | **HC** `pipeline.py` | assumption |
| label model: rule-accuracy prior mean / strength, bounds, EM iterations | 0.7 / 10, [0.05, 0.99], 50 | **HC** `label_model.py` | assumption |
| classifier | char 2-5-grams, min_df 2, C = 10 | **HC** `classifier.py` | assumption |
| rule amount cut-offs | UPI credit < Rs 20k = misc income; UPI debit < Rs 5k = misc expense; same-amount debit >= Rs 50k in 3+ months within 3 days = rent; counterparty paid >= Rs 10k 3+ times = supplier | **HC** `lfs.py` | assumption |

**Evidence** (run for this audit: `eval/models/categoriser_benchmark.py`, synthetic only,
n = 3,134 test transactions per set; "held out" = narration formats unseen in training):

| method | macro-F1 in template | macro-F1 held out | abstain held out |
|---|---|---|---|
| full cascade | 0.997 | 0.992 | 0.002 |
| rules + plain majority vote | 0.994 | 0.991 | 0.007 |
| classifier on gold labels | 1.000 | 0.652 | 0 |
| cascade's own classifier | 1.000 | 0.663 | 0 |

The label model and classifier add +0.001 macro-F1 on unseen formats over a plain majority
vote, and the classifier alone collapses there (0.65). On generator narrations the rules do
almost all the work. Real bank narrations are not measured.

---

## 3. Obligations detector

**Status:** default (inside `build_learned_inputs`).

**Maths.**
- **Statutory dates** come from the calendar in the file (`obligations/calendar.py:
  statutory_dates`).
- **TDS / PF / ESI amounts:** median of the last 6 payments of that kind. Confidence is
  1 minus their relative spread, where spread = median absolute deviation, MAD
  (`obligations/detector.py: _statutory_amount`).
- **GST:** date only (its amount is the sales forecaster's).
- **Salary, rent, EMI and fees** come from recurrence detection (`obligations/recurrence.py:
  _fit_group`):
  - Outflows are grouped by category and counterparty. A group needs 3 or more payments.
  - The period is whichever of 7 / 14 / 30 / 90 days best fits the median gap; the group is
    rejected if the gaps' MAD exceeds 25% of the period.
  - Amount = median of the last 6 payments.
  - Confidence = (1 - MAD / (0.25 P)) x min(1, n/6), with P the period and n the number of
    payments.

| constant | value | set in | status |
|---|---|---|---|
| statutory days | TDS 7th (March: 30 Apr), PF 15th, ESI 15th, GSTR-3B 20th, Sunday to Monday for GST | the file `statutory_calendar` | verified |
| QRMP GST day by state group | 22 / 24 | **HC** `calendar.py` QRMP_DAY | to verify (flagged) |
| candidate periods, max relative MAD, min payments, amount window | (7, 14, 30, 90), 0.25, 3, last 6 | **HC** `recurrence.py`, `detector.py` | assumption |

**Evidence:** unit tests only; no benchmark of detection rate or amount error is recorded.

---

## 4. Payment-timing model

**Status:** default, with the empirical mode on (`fit_timing(empirical=True)`).

**Maths.**
- **Model** (`timing/model.py`). Per buyer, a two-phase Beta-geometric with a cure fraction:
  - an invoice is paid each week with chance p_early before its due week and p from the due
    week on;
  - with chance pi it is never paid.
  - p_early, p and pi have Beta priors. Each unpaid week adds a failure to its phase and a
    payment adds a success.
- **Fitting:** pi needs Gibbs sampling over a hidden "dead" flag per open invoice, with
  P(dead | open c weeks) = pi / (pi + (1 - pi) S(c)), S = survival (`timing/model.py: gibbs`).
- **Group shrinkage** (`timing/hierarchy.py: group_prior`, `timing/pipeline.py: fit_timing`):
  1. Each buyer is first fitted under its type prior.
  2. Per (sector, type) group, a Beta is fitted to the members' posterior means by method of
     moments.
  3. Each buyer is refitted under that group prior.
- **Empirical mode** (`timing/empirical.py`), for buyers with 8 or more paid bills:
  - a weekly hazard per week relative to the due week (the chance of paying this week, given
    not paid yet), shrunk toward the buyer's own Beta-geometric chance;
  - used only up to the last week with at least 5 invoice-weeks at risk;
  - beyond that the engine uses each posterior draw's own p.
- **Labels** are cut at the ledger-complete-through watermark (`timing/labels.py`).

| constant | value | set in | status |
|---|---|---|---|
| type default priors | cpse p 0.30 / pi 0.01, listed 0.25 / 0.02, unlisted 0.20 / 0.05, p_early 0.05, all at strength 8 | **HC** `timing/hierarchy.py` TYPE_DEFAULTS | placeholder (for the buyer priors component) |
| prior strength cap | 8 | the file `payment_timing.prior_strength` | "tune" in the file, but never tuned: effectively an assumption |
| group-prior strength floor | 2 | **HC** `hierarchy.py` | assumption |
| Gibbs burn-in / draws | 50 / 400 | the file | tuned: R-hat < 1.01 on synthetic (20 gave 1.021) |
| never-paid cutoff | 365 days | the file | assumption ("proposed, not yet agreed") |
| empirical min paid bills | 8 | **HC** `timing/pipeline.py` EMPIRICAL_MIN_PAID | assumption |
| empirical min exposure | 5 invoice-weeks | the file | assumption |
| relative-week range | -26 to +26 (end bins hold everything beyond) | **HC** `timing/empirical.py` | assumption |
| time step, week origin | week, issue date | the file | assumption |

**Evidence (Kaggle, n = 5,955 test invoices):**

| model | C-index | integrated Brier score | ECE at 4 weeks |
|---|---|---|---|
| Beta-geometric only | 0.735 | 0.031 | 0.033 |
| with empirical mode | 0.741 | 0.027 | 0.028 |
| Cox | 0.770 | | |
| random survival forest | 0.785 | | |
| due date | 0.601 | | |

Group shrinkage: learned-mode calibration with shrinkage on and off gave mean |gap| 0.143 vs
0.143 (100 pasts). No measured benefit. A three-phase due-week variant was tried and dropped
(worse on every metric).

---

## 5. Sales forecaster

**Status:** default. Demand AR(1) and parameter uncertainty are on (`build_learned_inputs`
flags `demand_ar1=True`, `sales_uncertainty=True`).

**Maths** (`sales/model.py: fit_sales`, `SalesModel.sample`).
- **Weekly invoice count:** negative binomial, mean m and dispersion k by method of moments over
  complete weeks.
- **Buyer mix:** add-one-smoothed invoice shares.
- **Invoice details:** (amount, terms) pairs resampled from the buyer's own invoices, or from all
  invoices if the buyer has fewer than 10.
- **Purchase lag L:** the lag (0-30 days) that maximises the correlation of smoothed sales and
  supplier payments.
- **Purchase ratio:** supplier payments over invoices on windows offset by L.
- **Demand AR(1):** fitted from the count autocovariances; the current state comes from a Kalman
  filter (`_fit_demand`).
- **Parameter uncertainty:** each path uses one of 200 moving-block bootstrap replicates of
  (m, k, ratio).
- **GST:** 18% x (taxable sales - taxable purchases) per month, with excess credit carried
  forward (`sales/gst.py: gst_by_month`).

| constant | value | set in | status |
|---|---|---|---|
| min own invoices to resample | 10 | **HC** `sales/model.py` | assumption |
| max purchase lag searched | 30 days | **HC** | assumption |
| bootstrap replicates, block length | 200, 4 weeks | **HC** | assumption |
| dispersion cap (Poisson limit) | 1e4 | **HC** | logic |
| AR(1) rho clip | [0.05, 0.95] | **HC** | assumption |
| demand-level blocks | 8 weeks | **HC** | assumption |
| GST rate | 0.18 | **HC** default in `build_learned_inputs` and `io/adapters.invoices` | assumption, not in the file |

**Unused by default:** the persistent demand level (`level_sigma`, `demand_levels`) is fitted
but only used when `demand_ar1=False`.

**Evidence:**
- Kaggle weekly counts: negative binomial beats Poisson on log score (-4.41 vs -6.54, n = 90),
  but its 80% interval covers 0.97 (too wide).
- The 4-week block bootstrap changed learned calibration "almost nothing" (mean |gap| 0.143 vs
  0.143).
- No ablation of demand AR(1) or parameter uncertainty on the current pipeline is recorded.

---

## 6. Simulation engine

**Status:** default.

**Maths** (`engine/core.py: run_engine`). For each of N paths over H days:
1. One posterior draw per buyer.
2. Open invoices: dead with chance P(dead | survived to the cut); otherwise a payment week from
   the two-phase (or empirical) weekly chances, then a weekday from the buyer's weekday shares.
3. Future invoices and purchases from the sales forecaster, timed the same way.
4. GST per month and path from that path's sales and purchases.
5. Fixed obligations, overrides, scenario effects; then unscheduled flows.
6. Cumulative balance.

Sector shocks shift each week's chance on the log-odds scale by -z from a simulated AR(1) path.
The posterior is first converted to a "base" chance so that the shock-averaged chance equals
the posterior (`shocks/sector.py: base_chance`, Newton's method on 32-point Gauss-Hermite).

Outputs (`summarise`):
- daily quantiles 5/25/50/75/95;
- p_short(t) = share of paths that have been below the threshold by day t;
- expected shortfall;
- runway = first day with p_short >= 0.5.

Seeds come from run_id per component stream; `ENGINE_VERSION = "mc-v2"`.

**Shock component** (`shocks/sector.py`, `shocks/kalman.py`). Per sector:
- Weekly lateness z = -(payments - expected) / sum h(1 - h) over invoice-weeks at risk, where h
  is the expected chance of payment that week.
- A scalar AR(1) Kalman filter is fitted by grid search over rho and noise q (maximising the
  likelihood).
- Every buyer in the sector gets loading 1.
- Only sectors with 8 or more weeks of data get a fit.
- BOCPD (change-point detection) in the handbook is **not built**.

| constant | value | set in | status |
|---|---|---|---|
| paths, horizon | 10,000, 90 days | **HC** defaults in `run_engine` / `run_tenant`; the file's `simulation` block holds the same values but **nothing reads it** | logic (SE 0.002 at p = 0.05) / assumption |
| runway rule | first day p_short >= 0.5 | **HC** `engine/core.py summarise` | assumption |
| band quantiles | 0.05 ... 0.95 | **HC** `summarise` | contract-fixed |
| weekday shares | Mon-Sat uniform under 5 payments; +0.5 smoothing | **HC** `engine/learned.py` | assumption |
| shock grids | rho in {0, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95}; q 25 log-steps 1e-4 to 1 | **HC** `shocks/kalman.py` | assumption |
| shock: min weeks with data, loading | 8, 1 | **HC** `shocks/sector.py` | assumption |
| `shocks.kalman_rho / noise_scales / bocpd_hazard` | "tune" | the file | **unused**: rho and q are fitted per tenant; BOCPD not built |

**Evidence:**
- True-parameter mode matches the oracle within Monte Carlo error (largest |z| 2.1 over
  9 checks).
- Learned mode on 100 pasts: mean gap -0.045, mean |gap| 0.142, correlation 0.746. Low bins are
  under-predicted (0.12 vs 0.19).
- Base-logit shock correction on vs off: mean |gap| 0.133 vs 0.142 (on the ef5a591 pipeline).
- No recorded ablation of sector shocks as a whole.
- Berka outside check: engine AUC 0.639 vs 0.808 for a balance-only logistic regression
  (n = 682 loans). The engine loses to simple balance features.

---

## 7. Flow forecaster

**Status:** **off** (`build_learned_inputs(flow_forecaster="bootstrap")` is the default). The
engine's unscheduled flows by default are a same-weekday bootstrap over the **whole history**
(`full_unscheduled=True`, `engine/learned.py: bootstrap_unscheduled`). The module docstring in
`learned.py` still calls it a placeholder "until the flow forecaster exists" (stale).

**Maths** (`flows/forecaster.py: fit_flow_forecaster`, `FlowForecaster.sample`).
- **Quantile regression:** per unscheduled category, linear regression at the 5th, 25th, 50th,
  75th and 95th percentiles on:
  - weekday;
  - day-of-month band;
  - month-end;
  - an Indian festival window;
  - (yesterday's balance - its 180-day median) / interquartile range, so flows pull the balance
    back.
- **Persistence:** an AR(1) Gaussian copula per category. The uniforms used to sample from the
  quantiles are correlated day to day; rho comes from a randomised PIT (probability integral
  transform) of the history.
- **Feedback:** the engine samples these flows last, day by day, fed each path's own balance.
- **Short history:** under 8 weeks, Chronos-2 zero-shot quantiles (`flows/chronos.py`, optional
  extra `chronos`).

| constant | value | set in | status |
|---|---|---|---|
| quantiles | 0.05 ... 0.95 | **HC** `forecaster.py` QUANTILES; the file's `flows.quantiles` is **not read** | duplicate |
| Chronos fallback | under 8 weeks | the file | "tune" in the file; not tuned |
| festival dates | 30 dates 2022-2027 | the file | to verify |
| festival window, month-end days | 7 days before, last 3 days | **HC** | assumption |
| day-of-month bands | 1-5, 6-10, 11-15, 16-20, 21-25, 26+ | **HC** | assumption |
| usual-balance window, spread floor | 180 days, Rs 1 lakh | **HC** | assumption (90 days was tested and gave the wrong sign) |
| persistence cap | 0.9 | **HC** | assumption |

**Evidence (Berka, Kaggle copy):**
- Against same-weekday-last-week (n = 300 accounts): daily CRPS 0.61x, 90-day-sum CRPS 0.25x.
- Against the 8-week bootstrap: daily CRPS 0.90x (better in 96% of accounts). 30- and 90-day
  sums level (1.02x, 0.93x; intervals cross 0).
- Outside check: AUC 0.587 vs the bootstrap's 0.639, paired -0.051 (CI -0.088 to -0.014).
- Cause: lumpy inflows. For 90-day `misc_income` the model gives Rs 29k against Rs 48k realised.
- Calibration guard: -0.041 / 0.143 vs the bootstrap's -0.045 / 0.142.

---

## 8. Calibration

**Status:** **on demand**. Only the replay (`synth/replay.py`) carries a `Calibrator`;
`run.py` never calibrates, and the contract has nowhere to put a calibrated band
(questions 11-12 in the decisions doc).

**Maths** (`calibrate/aci.py: Calibrator`). Adaptive conformal inference (ACI), per horizon:
- alpha_{t+1} = alpha_t + gamma (alpha - err_t). Here err_t = 1 if the realised balance fell
  outside the band; a miss widens the band, a hit narrows it.
- The band is read off the engine's own paths.
- Feedback is delayed: a horizon-h band is scored when day t + h is known.
- It adapts only up to 30 days; 60 and 90 days keep the engine's band.
- Option `method="online"`: one ACI per gamma in a grid, weighted by pinball loss
  (Gibbs & Candes 2022).

| constant | value | set in | status |
|---|---|---|---|
| target coverage | 0.80 | the file | assumption |
| adapt up to | 30 days | the file | decided from the replay benchmark |
| gamma | 0.005 | the file `aci_gamma.paper_value` | paper value; the file's own note says it barely adapts at about 90 outcomes; not tuned |
| online grid, T | {0.005, 0.01, 0.02, 0.05, 0.1}, 365 | the file | assumption |
| horizons, miss window | (1, 7, 14, 30, 60, 90), 28 days | **HC** `aci.py` | assumption |

**Evidence** (3 worlds, n = 630 per horizon, heavily overlapping). Coverage of the 80% band:

| | 7 days | 14 days | 60 days | 90 days |
|---|---|---|---|---|
| engine's own band | 0.733 | 0.716 | 0.817 | 0.803 |
| ACI, gamma 0.05 | 0.790 | 0.778 | | |
| ACI, any gamma | | | 0.62-0.76 | 0.52-0.78 |
| online method | 0.752 | 0.738 | 0.617 | 0.649 |

ACI helps at 7 and 14 days and hurts at 60 and 90. The online method is worse than plain ACI
at every horizon.

---

## 9. Scenario engine

**Status:** **on demand** (`scenarios.run_scenario`, `breaking_point`; not in `run.py`).

**Maths** (`scenarios/transforms.py`, `apply.py`, `run.py`):
- **Transforms:** seven typed changes to the engine's inputs: rate multiplier, date shift,
  invoice cash, cash event, terms change, freeze, and order-rate multiplier.
- **Comparison:** each scenario runs on the baseline's seed (common random numbers, so the
  difference is the scenario and not noise).
- **Breaking point:** a 9-point grid from benign to severe checks that p_short rises with the
  magnitude, then bisection finds the target crossing.

| constant | value | set in | status |
|---|---|---|---|
| preset magnitudes | payments x 0.5; Rs 2 lakh expense on day 14; orders x 0.7; supplier terms 30 to 15 days; EMI 7 days earlier | `config/scenario_presets.yaml` | assumption (labelled placeholders) |
| purchase stickiness | 14 days | **HC** `transforms.py` DEFAULT_PURCHASE_STICKINESS_DAYS (also in the presets file) | assumption |
| breaking-point grid, tolerance, max iterations | 9, 1/1000 of range, 40 | **HC** `scenarios/run.py` | assumption |
| default search ranges | factor 1 to 0; cash 0 to 10x; buyer terms to 4x; shifts to 4x | **HC** `default_range` | assumption |

**Evidence:**
- The 365-day sustained order-cut sanity test passes.
- Default world: breaking points for delayed payments (factor 0.61) and supplier terms
  (15 days).
- "Reduced sales" lowers 90-day risk (fewer orders tie up less working capital) and reports as
  not monotone.

---

## 10. Decision engine

**Status:** **on demand** (`decide.decide`; not in `run.py`).

**Maths** (`decide/actions.py`, `decide/costs.py`, `decide/plan.py`).
- **Actions:** TReDS discount, chase a buyer, overdraft draw (whole tenths of the limit on 6
  possible days), and supplier stretch (7 / 14 / 30 days). Each is a scenario transform with an
  exact cost in paise.
- **MSMED interest:** P x (prod (1 + 3 r_k / 12) - 1), with r_k the bank rate at each monthly
  rest.
- **Stretch cost:** penalty + MSMED interest for the late days added + the 43B(h) deduction
  delay (tax x amount net of GST x cost of funds x financial years moved). Late means past the
  s.15 deadline: written terms capped at 45 days, else 15.
- **Optimiser:** a sample-average MILP on 1,000 paths minimises cost subject to at most
  floor(0.8 eps N) paths short by day D. It is solved by HiGHS, then checked on 10,000 fresh
  paths.
- **Claims:** MSMED claims are reported as "money owed", net of tax, recovery and relationship
  penalty, not planned.

| constant | value | set in | status |
|---|---|---|---|
| eps, day, solve factor | 0.05, 90, 0.8 | the file | decide / assumption / assumption |
| subsample, verify paths, time limit | 1,000, 10,000, 60 s | the file | logic / logic / assumption |
| bank rate history | 5.75% from 7 Oct 2026; 5.50% before (start date TODO) | the file `rbi_bank_rate` | verified (start date open) |
| MSMED multiplier, covered classes | 3, micro and small | **HC** `decide/costs.py` (the file's `msmed_act` has both, unread) | verified values, duplicated |
| s.15 deadline | min(terms, 45), 15 unwritten | **HC** `Supplier.msmed_deadline_days` (`io/adapters.resolve_terms` reads the same from the file) | verified values, duplicated; reading flagged VERIFY |
| TReDS rate, acceptance lag | 9%, 2 days | the file | to verify / assumption |
| chase | x 1.5 for 2 weeks, Rs 500 | the file | assumption |
| claim | lag 90 days, recovery 0.5, penalty 10% of principal | the file | to verify / assumption / decide |
| overdraft | 11%, draw days 1/15/30/45/60/75, 10 units | the file | to verify / assumption |
| stretch | 7/14/30 days, Rs 2,000 penalty | the file | assumption / decide |
| income-tax rate | company 25.17%; partnership and proprietor from the owner | the file | to verify / owner input |
| supplier default terms | 30 days | **HC** `Supplier` dataclass default | assumption |
| MILP scale | Rs 1,000 units | **HC** `plan.py` SCALE | numerics only |

**Evidence (30 worlds):**
- The plan meets eps strictly in 24/30 worlds (5/30 when solved at eps), and in 30/30 within
  Monte Carlo error.
- Mean cost Rs 61,899. Discount-all costs Rs 7,222 for p_short 0.387; do nothing gives 0.501.
- 5/30 solves stop at the time limit (largest MIP gap 0.87).
- No overdue invoice was worth claiming at default settings.

---

## 11. Attribution

**Status:** **stub** (`attribute/__init__.py`, docstring only). Nothing reports the invoices
and buyers driving the risk. The nearest thing is the decision engine's leave-one-out action
ranking (`decide/plan.py: loo`), which ranks actions, not drivers. The pieces it needs exist:
common random numbers via `seed_id`, and per-buyer and per-invoice transforms.

## 12. Buyer priors

**Status:** **stub** (`priors/__init__.py`). Nothing reads Schedule III disclosures. The
payment-timing model uses the placeholder `TYPE_DEFAULTS` (section 4). Evidence:
true generator hyperpriors instead of the placeholders improved learned calibration by only
0.004 (mean |gap| 0.143 to 0.139).

---

## Constants file entries that nothing reads

| entry | what the code does instead |
|---|---|
| `simulation.n_paths`, `simulation.horizon_days` | hard-coded defaults 10,000 / 90 |
| `flows.quantiles`, `flows.quantile_crossing_fix` | hard-coded in `flows/forecaster.py` |
| `shocks.kalman_rho`, `kalman_noise_scales`, `bocpd_hazard` | rho and q fitted per tenant by grid search; BOCPD not built |
| `msmed_act.section_16.rate_multiplier`, `msmed_act.applies_to` | hard-coded in `decide/costs.py` |
| `decision.relationship_penalty` (no value) | superseded by `msmed_claim.relationship_penalty_share` |
| `tds.rates.*` | only `matcher_candidate_rates` is read (by the generator) |
| `treds.discount_rate` (status decide) | `decision.treds.discount_rate_annual` 0.09 |

## Hard-coded values that belong in the file

- **GST rate 0.18:** in `build_learned_inputs` and `io/adapters.invoices`.
- **s.15 deadline (45 / 15 days) and MSMED multiplier 3:** in the decision engine. The adapters
  read the same from the file, so two copies of the same legal fact exist.
- **QRMP day map 22 / 24:** in `obligations/calendar.py` (flagged VERIFY in code, not in the file).
- **Timing type priors:** placeholders in `timing/hierarchy.py`.
- **Empirical-mode minimum of 8 paid bills.**
- **Shock fit minimum of 8 weeks; loading 1.**
- **Runway rule p_short >= 0.5.**
- **Generator:** TDS 10% on the CA fee, and the unscheduled-flow amount ranges.

---

## Simplify

Each item: what to cut, the simpler version, and what we would lose, with the evidence.

1. **Categoriser cascade → rules + majority vote + owner review of abstains.**
   - *Why:* on held-out formats, majority vote 0.991 against the full cascade 0.992 (n = 3,134).
     The classifier alone is 0.65 there.
   - *Lose:* about 0.001-0.003 macro-F1 and 0.5 points of abstain rate on generator text. Real
     narrations are unmeasured, so keep the label model code until a real labelled sample says
     otherwise.
2. **Timing group shrinkage → type priors only.**
   - *Why:* no measured benefit (mean |gap| 0.143 with and without), and it doubles the Gibbs
     work (every buyer is fitted twice).
   - *Lose:* possibly better estimates for buyers with 1-3 invoices; no test covers that yet.
3. **Flow forecaster → keep the full-history bootstrap as the only unscheduled source until
   a two-part model is built.**
   - *Why:* the quantile forecaster is off by default, loses the outside check by 0.051 AUC and
     ties the bootstrap on 30- and 90-day sums. It adds balance feedback, which makes the
     decision engine's deltas approximate.
   - *Lose:* 10% better daily CRPS, and Chronos-2 for new tenants (the bootstrap needs history
     too, so this matters for onboarding).
4. **Calibration "online" method → drop it.**
   - *Why:* worse than plain ACI at every horizon in the benchmark, and calibration itself is
     not in the production path.
   - *Simpler still:* skip ACI and report the engine band, accepting under-coverage at
     7-14 days (0.72-0.73 against 0.80).
   - *Lose:* a hedge against a wrongly chosen gamma.
5. **Sales demand variability: three mechanisms → one.**
   - *What there is:* the persistent demand level (fitted, unused by default), the demand AR(1),
     and the block-bootstrapped mean. They overlap; the sales docstring says the level double
     counts.
   - *Simpler:* AR(1) plus the bootstrap, and delete the level code, or bootstrap only.
   - *Lose:* nothing measured; no ablation recorded. Run one before choosing.
6. **Empirical timing mode: keep, but note its cost.**
   - *Gain:* +0.006 C-index and -0.004 Brier on Kaggle.
   - *Cost:* a second timing path in the engine (hazard matrix, NaN tails, data extent), and it
     caused the 6e07b3d calibration regression.
   - *Simpler:* Beta-geometric only.
   - *Lose:* the small but consistent Kaggle gain.
7. **Decision engine search space → fewer chase columns.**
   - *Why:* 5/30 solves hit the 60 s limit, one far from optimal.
   - *Columns that could go:* the chase x invoice interaction variables, and chase candidates
     for small buyers.
   - *Keep the overdraft draw days:* every plan draws overdraft, and the draws use all the days
     (day 1 in 12 plans, day 30 in 10, day 45 in 15, day 60 in 19, day 75 in 7 of 30), so a
     single draw day would cost interest.
   - *Lose:* some plans would chase less precisely; size unknown until the 30-world rerun.
8. **Constants hygiene (no behaviour change).**
   - Delete or wire the unread file entries.
   - Read GST rate, s.15 days and the MSMED multiplier from the file in one place.
   - Fix the stale `learned.py` docstring.
   - *Lose:* nothing.
9. **Stubs (`priors/`, `attribute/`): keep as placeholders.** They are on the critical path for
   gates G2 and G3, not dead code. Attribution needs only re-runs on common random numbers, which
   already exist.
