"""Real-data checks on the Kaggle invoice payment dataset (benchmarks only, SOURCES.md).

    uv run --project intelligence --extra bench python eval/models/kaggle_checks.py [timing|sales|synth|all]

1. timing: payment-timing model vs due date, Cox, random survival forest and a LightGBM
   person-period (discrete weekly hazard) model. Temporal split: fit on invoices issued
   before AS_OF (labels as known at AS_OF, open ones censored there), test on invoices issued
   in [AS_OF, TEST_END). Test outcomes are followed to FOLLOW_DAYS. Metrics: C-index
   (bootstrap 95% CI), integrated Brier over weeks 1..15, calibration of P(paid within
   4/8/12 weeks). Never-paid calibration is not measurable: the dataset holds no unpaid
   invoices (right-truncated at the fiscal year end, SOURCES.md).
2. sales: weekly invoice counts, overall and per customer: negative binomial vs Poisson,
   rolling one-week-ahead backtest, log score and 80% interval coverage.
3. synth: a gradient-boosted classifier telling generator invoices from Kaggle invoices on
   matched, scale-free features; AUC and permutation importances.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loaders  # noqa: E402
import results  # noqa: E402

from matsya_intelligence.io import adapters  # noqa: E402
from matsya_intelligence.seeding import rng_for  # noqa: E402
from matsya_intelligence.timing import fit_timing  # noqa: E402
from matsya_intelligence.timing.model import Posterior, predict_open  # noqa: E402

AS_OF = date(2019, 7, 1)
TEST_END = date(2019, 9, 1)  # test issues >= 17 weeks before the 2019-12-31 truncation
FOLLOW_DAYS = 112  # 16 weeks
TENANTS = ("U001", "CA02", "U013")  # business codes with >= 400 invoices
GRID = np.arange(FOLLOW_DAYS + 1)  # day t: P(paid by end of day t after issue)
HORIZONS = {4: 27, 8: 55, 12: 83}  # weeks -> last day inside the horizon
IBS_TIMES = np.arange(7, 106, 7)
SEED = "kaggle-checks-v1"


# ---------------------------------------------------------------- data


def _invoice_frame(raw: pl.DataFrame) -> pl.DataFrame:
    d = raw.filter(pl.col("business_code").is_in(TENANTS)).with_columns(
        (pl.col("clear_date") - pl.col("issue_date")).dt.total_days().alias("time"),
        (pl.col("due_date") - pl.col("issue_date")).dt.total_days().alias("terms"),
        pl.col("amount").log().alias("log_amount"),
        (pl.col("currency") == "CAD").cast(pl.Int8).alias("cad"),
        pl.col("issue_date").dt.weekday().alias("issue_wd"),
    )
    return d


def _customer_history(d: pl.DataFrame) -> pl.DataFrame:
    """Per invoice, stats of the same customer's invoices cleared before min(issue, AS_OF):
    no information after AS_OF reaches any model."""
    paid = d.select("business_code", "cust_number", "clear_date", "time",
                    (pl.col("clear_date") - pl.col("due_date")).dt.total_days().alias("late")).sort("clear_date")
    out = []
    for (bc, cust), g in d.group_by("business_code", "cust_number"):
        p = paid.filter((pl.col("business_code") == bc) & (pl.col("cust_number") == cust))
        pd_, pt, pl_ = p["clear_date"].to_numpy(), p["time"].to_numpy(), p["late"].to_numpy()
        for doc, issue in zip(g["doc_id"], g["issue_date"]):
            k = np.searchsorted(pd_, np.datetime64(min(issue, AS_OF)), side="left")
            out.append((bc, doc, k, float(pt[:k].mean()) if k else np.nan, float(pl_[:k].mean()) if k else np.nan,
                        float((pl_[:k] > 7).mean()) if k else np.nan))
    h = pl.DataFrame(out, schema={"business_code": pl.String, "doc_id": pl.String, "hist_n": pl.Int64,
                                  "hist_time": pl.Float64, "hist_late": pl.Float64, "hist_late7": pl.Float64},
                     orient="row")
    return d.join(h, on=["business_code", "doc_id"])


FEATURES = ["terms", "log_amount", "cad", "issue_wd", "hist_n", "hist_time", "hist_late", "hist_late7",
            "bc_U001", "bc_CA02"]


def _design(d: pl.DataFrame, fill: dict | None = None) -> tuple[np.ndarray, dict]:
    d = d.with_columns((pl.col("business_code") == "U001").cast(pl.Int8).alias("bc_U001"),
                       (pl.col("business_code") == "CA02").cast(pl.Int8).alias("bc_CA02"))
    fill = fill or {c: float(d[c].drop_nans().drop_nulls().median() or 0.0) for c in FEATURES}
    X = d.select([pl.col(c).cast(pl.Float64).fill_nan(None).fill_null(fill[c]) for c in FEATURES]).to_numpy()
    return X, fill


def split():
    d = _customer_history(_invoice_frame(loaders.kaggle_raw()))
    train = d.filter(pl.col("issue_date") < AS_OF).with_columns(
        (pl.col("clear_date") <= AS_OF).alias("event"),
        pl.when(pl.col("clear_date") <= AS_OF).then(pl.col("time"))
        .otherwise((pl.lit(AS_OF) - pl.col("issue_date")).dt.total_days()).alias("obs_time"))
    test = d.filter(pl.col("issue_date").is_between(AS_OF, TEST_END, closed="left")).with_columns(
        (pl.col("time") <= FOLLOW_DAYS).alias("event"), pl.col("time").clip(upper_bound=FOLLOW_DAYS).alias("obs_time"))
    return d, train, test


# ---------------------------------------------------------------- models -> CDF on GRID


def _weekly_to_daily(paid_weeks: np.ndarray) -> np.ndarray:
    """P(paid by end of day t), t in GRID, from P(paid in week w), payment day uniform in week."""
    cum = np.concatenate([[0.0], np.cumsum(paid_weeks)])
    w = GRID // 7
    return cum[w] + paid_weeks[w] * ((GRID % 7) + 1) / 7


def ours(test: pl.DataFrame) -> np.ndarray:
    """Payment-timing model, fitted per tenant on the as-of contract tables via the adapters."""
    raw = loaders.kaggle_raw()
    n_weeks = FOLLOW_DAYS // 7 + 1
    cdf = np.zeros((test.height, GRID.size))
    for bc in TENANTS:
        tables = loaders.kaggle_tables(raw, bc, AS_OF)
        out, inv, buyers = adapters.outcomes(tables, AS_OF), adapters.invoices(tables, AS_OF), adapters.buyers(
            tables, loaders.kaggle_tenant_id(bc))
        tm = fit_timing(out, inv, buyers, adapters.ledger_complete_through(tables, AS_OF), run_id=f"{SEED}:{bc}")
        prior = next(iter(tm.priors.values()))
        r = rng_for(SEED, f"unseen:{bc}")
        unseen = Posterior(p=r.beta(prior.alpha, prior.beta, 400), pi=r.beta(prior.a, prior.b, 400),
                           p_early=r.beta(prior.alpha_e, prior.beta_e, 400))
        tenant = loaders.kaggle_tenant_id(bc)
        from matsya_contracts import ids

        for i, row in enumerate(test.iter_rows(named=True)):
            if row["business_code"] != bc:
                continue
            post = tm.posteriors.get(ids.local_buyer_id(tenant, row["cust_number"]), unseen)
            paid, _, _ = predict_open(post, 0, n_weeks + 1, row["terms"] // 7 + 1)
            cdf[i] = _weekly_to_daily(paid)
    return cdf


def due_date(test: pl.DataFrame) -> np.ndarray:
    return (GRID[None, :] >= test["terms"].to_numpy()[:, None]).astype(float)


def _surv_y(df: pl.DataFrame):
    from sksurv.util import Surv

    return Surv.from_arrays(df["event"].to_numpy(), np.maximum(df["obs_time"].to_numpy(), 0.5))


def cox(train, test) -> np.ndarray:
    from sklearn.preprocessing import StandardScaler
    from sksurv.linear_model import CoxPHSurvivalAnalysis

    Xtr, fill = _design(train)
    Xte, _ = _design(test, fill)
    sc = StandardScaler().fit(Xtr)
    m = CoxPHSurvivalAnalysis(alpha=1e-2).fit(sc.transform(Xtr), _surv_y(train))
    return _from_step(m.predict_survival_function(sc.transform(Xte)))


RSF_GRID = [("sqrt", 30), (None, 15), (None, 50), (0.5, 15)]
INNER_AS_OF = date(2019, 5, 1)


def _inner_split(train: pl.DataFrame):
    """Validation inside the training period: fit on issues before INNER_AS_OF (censored there),
    score on issues in [INNER_AS_OF, AS_OF) as known at AS_OF. Never touches the test set."""
    fit = train.filter(pl.col("issue_date") < INNER_AS_OF).with_columns(
        (pl.col("clear_date") <= INNER_AS_OF).alias("event"),
        pl.when(pl.col("clear_date") <= INNER_AS_OF).then(pl.col("time"))
        .otherwise((pl.lit(INNER_AS_OF) - pl.col("issue_date")).dt.total_days()).alias("obs_time"))
    val = train.filter(pl.col("issue_date") >= INNER_AS_OF)
    return fit, val


def rsf(train, test) -> np.ndarray:
    """Random survival forest; max_features and leaf size picked by C-index on the inner split."""
    from sksurv.ensemble import RandomSurvivalForest
    from sksurv.metrics import concordance_index_censored

    def make(mf, leaf):
        return RandomSurvivalForest(n_estimators=200, min_samples_leaf=leaf, max_features=mf, n_jobs=-1,
                                    random_state=0)

    fit, val = _inner_split(train)
    Xf, ff = _design(fit)
    Xv, _ = _design(val, ff)
    scores = {}
    for mf, leaf in RSF_GRID:
        cdf = _from_step(make(mf, leaf).fit(Xf, _surv_y(fit)).predict_survival_function(Xv))
        scores[(mf, leaf)] = concordance_index_censored(val["event"].to_numpy(), val["obs_time"].to_numpy().astype(float),
                                                        cdf.sum(axis=1))[0]
    best = max(scores, key=scores.get)
    print("rsf inner-validation C-index", scores, "chosen", best, flush=True)
    Xtr, fill = _design(train)
    Xte, _ = _design(test, fill)
    return _from_step(make(*best).fit(Xtr, _surv_y(train)).predict_survival_function(Xte))


def _from_step(fns) -> np.ndarray:
    out = np.empty((len(fns), GRID.size))
    for i, f in enumerate(fns):
        x = np.clip(GRID, f.x[0], f.x[-1])
        out[i] = 1 - f(x)
        out[i, GRID < f.x[0]] = 0.0
    return out


def lgbm_person_period(train, test) -> np.ndarray:
    """Discrete weekly hazard: one row per invoice-week at risk; S(w) = prod (1 - h)."""
    import lightgbm as lgb

    Xtr, fill = _design(train)
    Xte, _ = _design(test, fill)
    t, e = train["obs_time"].to_numpy(), train["event"].to_numpy()
    wk = t // 7 + 1  # week of event or censoring
    rows, ys = [], []
    for i in range(len(t)):
        last = wk[i] if e[i] else max(wk[i] - 1, 0)  # censored mid-week: that week not fully at risk
        for w in range(1, last + 1):
            rows.append(np.concatenate([Xtr[i], [w, w - (Xtr[i, 0] // 7 + 1)]]))
            ys.append(1 if (e[i] and w == wk[i]) else 0)
    m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=50,
                           random_state=0, verbose=-1).fit(np.array(rows), np.array(ys))
    n_weeks = FOLLOW_DAYS // 7 + 1
    paid = np.zeros((len(Xte), n_weeks + 1))
    surv = np.ones(len(Xte))
    for w in range(1, n_weeks + 2):
        h = m.predict_proba(np.column_stack([Xte, np.full(len(Xte), w), w - (Xte[:, 0] // 7 + 1)]))[:, 1]
        paid[:, w - 1] = surv * h
        surv = surv * (1 - h)
    return np.array([_weekly_to_daily(p) for p in paid])


# ---------------------------------------------------------------- metrics


def c_index(test, cdf, rng=None) -> float:
    from sksurv.metrics import concordance_index_censored

    risk = cdf.sum(axis=1)  # higher = paid earlier
    ev, tm = test["event"].to_numpy(), test["obs_time"].to_numpy().astype(float)
    if rng is None:
        return concordance_index_censored(ev, tm, risk)[0]
    i = rng.integers(0, len(ev), len(ev))
    return concordance_index_censored(ev[i], tm[i], risk[i])[0]


def ibs(train, test, cdf) -> float:
    from sksurv.metrics import integrated_brier_score

    surv = 1 - cdf[:, IBS_TIMES]
    return integrated_brier_score(_surv_y(train), _surv_y(test), surv, IBS_TIMES)


def calibration(test, cdf, day) -> tuple[float, float, float, float]:
    """(mean predicted, observed rate, ECE over deciles, Brier) for P(paid by day)."""
    p = cdf[:, day]
    y = (test["obs_time"].to_numpy() <= day) & test["event"].to_numpy()
    edges = np.quantile(p, np.linspace(0, 1, 11))
    b = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, 9)
    ece = sum(abs(p[b == k].mean() - y[b == k].mean()) * (b == k).sum() for k in range(10) if (b == k).any()) / len(p)
    return float(p.mean()), float(y.mean()), float(ece), float(((p - y) ** 2).mean())


def check_timing() -> tuple[list[dict], str]:
    d, train, test = split()
    models = {"payment_timing_model": lambda: ours(test), "due_date": lambda: due_date(test),
              "cox": lambda: cox(train, test), "random_survival_forest": lambda: rsf(train, test),
              "lgbm_person_period": lambda: lgbm_person_period(train, test)}
    early = d.filter(pl.col("issue_date") < date(2019, 4, 1))
    trunc = {k: float((early["time"] > k).mean()) for k in (83, 112)}
    rows, table = [], []
    rng = rng_for(SEED, "bootstrap")
    for name, fn in models.items():
        cdf = np.clip(fn(), 0, 1)
        ci = c_index(test, cdf)
        boots = [c_index(test, cdf, np.random.default_rng(rng.integers(2**32))) for _ in range(200)]
        lo, hi = np.quantile(boots, [0.025, 0.975])
        score = ibs(train, test, cdf)
        bench = dict(benchmark="kaggle_payment_timing", dataset="kaggle_payment_date_v2", component="payment_timing",
                     model=name, baseline="due_date")
        rows += [results.row(metric="c_index", value=ci, n=test.height, ci=(lo, hi), **bench),
                 results.row(metric="integrated_brier", value=score, n=test.height, horizon="1-15w", **bench)]
        rec = {"model": name, "c_index": ci, "c_index_ci": f"{lo:.3f}-{hi:.3f}", "ibs_1_15w": score}
        for wks, day in HORIZONS.items():
            mp, obs, ece, br = calibration(test, cdf, day)
            rows += [results.row(metric="calib_mean_pred", value=mp, n=test.height, horizon=f"{wks}w", **bench),
                     results.row(metric="calib_observed", value=obs, n=test.height, horizon=f"{wks}w", **bench),
                     results.row(metric="calib_ece_decile", value=ece, n=test.height, horizon=f"{wks}w", **bench),
                     results.row(metric="brier", value=br, n=test.height, horizon=f"{wks}w", **bench)]
            rec |= {f"pred_{wks}w": mp, f"obs_{wks}w": obs, f"ece_{wks}w": ece}
        table.append(rec)
        print(rec, flush=True)
    rows.append(results.row("kaggle_payment_timing", "kaggle_payment_date_v2", "payment_timing", "all",
                            "never_paid_calibration", None, 0, "n/a",
                            note="not measurable: dataset holds no unpaid invoices (right-truncated)"))
    md = (f"# Kaggle payment timing (benchmark only)\n\nTrain: invoices issued before {AS_OF} "
          f"(n = {train.height}, paid by then {int(train['event'].sum())}, censored {int((~train['event']).sum())}), "
          f"tenants {', '.join(TENANTS)}. Test: issued {AS_OF} to {TEST_END - timedelta(days=1)} "
          f"(n = {test.height}), followed {FOLLOW_DAYS} days. Baseline: due date.\n\n"
          + results.md_table(pl.DataFrame(table)) +
          f"\n\nRight truncation: of invoices issued Jan-Mar 2019 (n = {early.height}), "
          f"{trunc[83]:.4f} took more than 83 days and {trunc[112]:.4f} more than 112 days, so the "
          "fiscal-year cut removes little at these horizons. Never-paid calibration is not measurable: "
          "the dataset holds no unpaid invoices.\n")
    return rows, md


# ---------------------------------------------------------------- 2. sales forecaster counts

SALES_FIRST = date(2019, 3, 4)  # first origin (a Monday): >= 8 weeks of history
SALES_LAST = date(2019, 9, 23)  # last origin: its week ends before Q4, where truncation thins counts
MIN_CUSTOMER_INVOICES = 20


def _nb_pmf_cdf(y: np.ndarray, m: np.ndarray, k: np.ndarray):
    from scipy.stats import nbinom, poisson

    poi = k >= 1e4 - 1
    p = np.where(poi, 0.5, k / (k + m))
    kk = np.where(poi, 1.0, k)
    logpmf = np.where(poi, poisson.logpmf(y, m), nbinom.logpmf(y, kk, p))
    lo = np.where(poi, poisson.ppf(0.1, m), nbinom.ppf(0.1, kk, p))
    hi = np.where(poi, poisson.ppf(0.9, m), nbinom.ppf(0.9, kk, p))
    return logpmf, lo, hi


def check_sales() -> tuple[list[dict], str]:
    """Rolling one-week-ahead backtest of the sales forecaster's weekly count model.

    At each Monday origin the sales forecaster is fitted (fit_sales, through the adapters) on
    invoices issued before it; the next 7 days' count is scored under NegBin(m, k) and under
    Poisson(m) with the same m. Per customer: thinning the total by the forecaster's buyer
    mix share s gives NegBin(m s, k) vs Poisson(m s), for customers with >= 20 past invoices.
    """
    from matsya_contracts import ids
    from matsya_intelligence.sales import fit_sales

    raw = loaders.kaggle_raw()
    cats = pl.DataFrame(schema={"txn_uid": pl.String, "category": pl.String})
    origins = [SALES_FIRST + timedelta(weeks=i) for i in range((SALES_LAST - SALES_FIRST).days // 7 + 1)]
    acc = {(lvl, mdl): {"ls": [], "cov": []} for lvl in ("overall", "customer") for mdl in ("negbin", "poisson")}
    ks = []
    for bc in TENANTS:
        tables = loaders.kaggle_tables(raw, bc, SALES_LAST + timedelta(days=7))
        allinv = adapters.invoices(tables, SALES_LAST + timedelta(days=7))
        for o in origins:
            inv = adapters.invoices(tables, o)
            sm = fit_sales(inv, adapters.transactions(tables, o), cats, o)
            nxt = allinv.filter(pl.col("issue_date").is_between(o, o + timedelta(days=6)))
            ks.append(sm.dispersion)
            past = inv.filter(pl.col("issue_date") < o).group_by("buyer_id").len()
            big = past.filter(pl.col("len") >= MIN_CUSTOMER_INVOICES)["buyer_id"].to_list()
            share = dict(zip(sm.buyer_ids, sm.mix))
            ycust = dict(zip(*nxt.group_by("buyer_id").len().to_dict(as_series=False).values()))
            levels = {"overall": (np.array([nxt.height]), np.array([sm.mean_per_week])),
                      "customer": (np.array([ycust.get(b, 0) for b in big]),
                                   np.array([sm.mean_per_week * share[b] for b in big]))}
            for lvl, (y, m) in levels.items():
                if y.size == 0:
                    continue
                for mdl, k in (("negbin", np.full(y.size, sm.dispersion)), ("poisson", np.full(y.size, 1e4))):
                    lp, lo, hi = _nb_pmf_cdf(y, m, k)
                    acc[(lvl, mdl)]["ls"] += list(lp)
                    acc[(lvl, mdl)]["cov"] += list((y >= lo) & (y <= hi))
    rows, table = [], []
    for (lvl, mdl), a in acc.items():
        n = len(a["ls"])
        bench = dict(benchmark="kaggle_sales_counts", dataset="kaggle_payment_date_v2", component="sales_forecaster",
                     model=mdl, baseline="poisson", horizon=f"1w:{lvl}")
        rows += [results.row(metric="mean_log_score", value=np.mean(a["ls"]), n=n, **bench),
                 results.row(metric="coverage_80", value=np.mean(a["cov"]), n=n, **bench)]
        table.append({"level": lvl, "model": mdl, "n": n, "mean_log_score": float(np.mean(a["ls"])),
                      "coverage_80": float(np.mean(a["cov"]))})
    md = (f"# Kaggle weekly invoice counts: sales forecaster (benchmark only)\n\nRolling one-week-ahead origins "
          f"{SALES_FIRST} to {SALES_LAST} (weekly, {len(origins)} per tenant), tenants {', '.join(TENANTS)}. "
          f"Customer level: customers with >= {MIN_CUSTOMER_INVOICES} invoices before the origin. Higher log score "
          f"is better; nominal coverage 0.80. Fitted dispersion k: median {np.median(ks):.1f} "
          f"(range {min(ks):.1f}-{max(ks):.1f}). Baseline: Poisson with the same mean.\n\n"
          + results.md_table(pl.DataFrame(table)) + "\n")
    return rows, md


# ---------------------------------------------------------------- 3. synthetic vs real

SYNTH_PAST_SEEDS = range(5)
MATCH_FEATURES = ["log_amount_rel", "terms", "days_to_pay", "days_late", "customer_part_pay_rate",
                  "week_count_rel", "customer_share"]


def _matched(inv: pl.DataFrame, group: str) -> pl.DataFrame:
    """Scale-free features per paid invoice. inv: group, customer, issue_date, due_date, paid_date,
    amount, part_paid. Only invoices paid within FOLLOW_DAYS (the Kaggle truncation)."""
    wk = pl.col("issue_date").dt.truncate("1w")
    return (inv.with_columns(
        (pl.col("amount") / pl.col("amount").median().over(group)).log().alias("log_amount_rel"),
        (pl.col("due_date") - pl.col("issue_date")).dt.total_days().alias("terms"),
        (pl.col("paid_date") - pl.col("issue_date")).dt.total_days().alias("days_to_pay"),
        (pl.col("paid_date") - pl.col("due_date")).dt.total_days().alias("days_late"),
        pl.col("part_paid").cast(pl.Float64).mean().over(group, "customer").alias("customer_part_pay_rate"),
        (pl.len().over(group, wk) / (pl.len().over(group) / pl.col("issue_date").dt.truncate("1w").n_unique().over(group)))
        .alias("week_count_rel"),
        (pl.len().over(group, "customer") / pl.len().over(group)).alias("customer_share"),
    ).filter(pl.col("days_to_pay").is_between(0, FOLLOW_DAYS)))


def _synthetic_invoices() -> pl.DataFrame:
    from matsya_intelligence.synth import WorldConfig, generate_world

    frames = []
    for s in SYNTH_PAST_SEEDS:
        cfg = WorldConfig()
        _, truth = generate_world(cfg, s, 0)
        frames.append(truth.invoices.filter(pl.col("paid_date").is_not_null() & (pl.col("issue_date") <= cfg.as_of_date))
                      .select(pl.lit(f"synth{s}").alias("group"), pl.col("buyer_id").alias("customer"), "issue_date",
                              "due_date", "paid_date", pl.col("gross_paise").cast(pl.Float64).alias("amount"),
                              pl.col("part_paid").fill_null(False)))
    return pl.concat(frames)


def _kaggle_invoices() -> pl.DataFrame:
    return (loaders.kaggle_raw().filter(pl.col("business_code").is_in(TENANTS)
                                        & pl.col("issue_date").is_between(date(2019, 1, 1), date(2019, 9, 30)))
            .select(pl.col("business_code").alias("group"), pl.col("cust_number").alias("customer"), "issue_date",
                    "due_date", pl.col("clear_date").alias("paid_date"), "amount", pl.lit(False).alias("part_paid")))


def check_synth_vs_real() -> tuple[list[dict], str]:
    """Can a classifier tell generator invoices from Kaggle invoices? AUC near 0.5 means the
    generator matches on these features; the top features are what it gets wrong."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold

    syn = _matched(_synthetic_invoices(), "group").with_columns(pl.lit(1).alias("y"))
    real = _matched(_kaggle_invoices(), "group").with_columns(pl.lit(0).alias("y"))
    df = pl.concat([syn.select(MATCH_FEATURES + ["y"]), real.select(MATCH_FEATURES + ["y"])])
    y = df["y"].to_numpy()
    rows, mds = [], []
    for variant, feats in (("all_features", MATCH_FEATURES),
                           ("without_part_pay", [f for f in MATCH_FEATURES if f != "customer_part_pay_rate"]),
                           ("invoice_level", ["log_amount_rel", "terms", "days_to_pay", "days_late", "week_count_rel"])):
        X = df.select(feats).to_numpy().astype(float)
        pred = np.zeros(len(y))
        imp = np.zeros(len(feats))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
            m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, random_state=0).fit(X[tr], y[tr])
            pred[te] = m.predict_proba(X[te])[:, 1]
            sub = te[np.random.default_rng(0).choice(te.size, min(4000, te.size), replace=False)]
            imp += permutation_importance(m, X[sub], y[sub], scoring="roc_auc", n_repeats=5,
                                          random_state=0).importances_mean / 5
        auc = roc_auc_score(y, pred)
        uni = {f: max(roc_auc_score(y, df[f].to_numpy()), 1 - roc_auc_score(y, df[f].to_numpy())) for f in feats}
        bench = dict(benchmark="synthetic_vs_real", dataset="kaggle_payment_date_v2+generator", component="generator",
                     baseline="auc_0.5_indistinguishable")
        rows.append(results.row(model=f"hist_gbm:{variant}", metric="auc_5fold", value=auc, n=len(y), **bench,
                                note=f"synthetic {int(y.sum())}, real {int((1 - y).sum())}"))
        order = np.argsort(-imp)
        tab = []
        for i in order:
            rows.append(results.row(model=f"hist_gbm:{variant}", metric="perm_importance_auc", horizon=feats[i],
                                    value=imp[i], n=len(y), **bench))
            tab.append({"feature": feats[i], "perm_importance_auc_drop": float(imp[i]),
                        "univariate_auc": float(uni[feats[i]])})
        mds.append(f"## {variant}: AUC {auc:.3f} (5-fold CV, n = {len(y)}: synthetic {int(y.sum())}, "
                   f"real {int((1 - y).sum())})\n\n" + results.md_table(pl.DataFrame(tab)))
    desc = pl.concat([syn.select(MATCH_FEATURES).describe().with_columns(pl.lit("synthetic").alias("source")),
                      real.select(MATCH_FEATURES).describe().with_columns(pl.lit("kaggle").alias("source"))]).filter(
        pl.col("statistic").is_in(["mean", "std", "25%", "50%", "75%"]))
    md = ("# Synthetic vs real invoices (benchmark only)\n\nGenerator: default (borderline) worlds, past seeds "
          f"{list(SYNTH_PAST_SEEDS)}, paid invoices issued by as_of. Real: Kaggle tenants {', '.join(TENANTS)}, "
          "issued 2019-01-01 to 2019-09-30. Both cut at days to pay <= 112 (the Kaggle truncation). Features are "
          "scale-free (amount relative to the tenant median, weekly count relative to the tenant mean). Kaggle has "
          "no part payments, so the part-payment rate separates trivially; the second variant drops it. Kaggle tenants "
          "have about 600 customers each against the generator's 10 buyers, so customer share is structural too; the "
          "third variant keeps only per-invoice features.\n\n"
          + "\n\n".join(mds) + "\n\n## Feature distributions\n\n" + results.md_table(desc, 2) + "\n")
    return rows, md


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    all_rows, mds = [], []
    if which in ("timing", "all"):
        r, m = check_timing()
        all_rows += r
        mds.append(m)
    if which in ("sales", "all"):
        r, m = check_sales()
        all_rows += r
        mds.append(m)
    if which in ("synth", "all"):
        r, m = check_synth_vs_real()
        all_rows += r
        mds.append(m)
    results.write(all_rows, "\n".join(mds), "kaggle_" + which)
