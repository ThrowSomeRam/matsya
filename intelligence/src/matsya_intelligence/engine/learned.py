"""Build engine inputs from an as_of snapshot and the fitted components (learned mode).

Payment-timing model -> posterior draws and open invoices; sales forecaster ->
future invoices and purchases (with a persistent demand level per path);
obligations detector -> fixed obligations and GST dates; GST amounts per path
from sales and purchases (sales forecaster owns them); unscheduled flows from
the placeholder below until the flow forecaster exists.

Unscheduled placeholder: for each future day, the net unscheduled flow of a
random past day with the same weekday, from the last 8 weeks or (full_unscheduled)
the whole history, so rare large flows can appear.

Flags (each was a calibration step; see eval/models/engine_calibration.py):
  sales_uncertainty: per-path bootstrap draws of order rate, dispersion, purchase ratio
  sector_shocks:     per-path sector lateness paths from the shock component
  demand_ar1:        AR(1) on log order rate instead of one persistent level per path
  full_unscheduled:  bootstrap unscheduled flows from the full history
  naive_purchases:   (only with with_sales=False) supplier payments as a flat daily
                     outflow at their historical mean, as a naive tool would
  timing_priors, timing_hierarchical: diagnostics only (e.g. the generator's true
                     type hyperpriors, no group shrinkage)
"""

import hashlib
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.engine.core import EngineInputs, Invoices, TimingDraws, month_key
from matsya_intelligence.obligations import detect_obligations
from matsya_intelligence.sales import fit_sales
from matsya_intelligence.shocks import fit_sector_shocks
from matsya_intelligence.synth.world import PROCESS_OF_CATEGORY
from matsya_intelligence.timing import fit_timing

UNSCHEDULED = [c for c, p in PROCESS_OF_CATEGORY.items() if p == "unscheduled"]
MON_SAT = np.array([1, 1, 1, 1, 1, 1, 0], dtype=float) / 6
MIN_WEEKDAY_PAYMENTS = 5
BOOTSTRAP_WEEKS = 8


def _snapshot_id(df: pl.DataFrame) -> str:
    return hashlib.sha256(df.write_csv().encode()).hexdigest()[:16]


def weekday_probs(outcomes: pl.DataFrame, buyer_ids: list[str]) -> np.ndarray:
    """Per buyer: weekday shares of past payment dates (+0.5 smoothing), or uniform Mon-Sat under 5 payments."""
    out = np.tile(MON_SAT, (len(buyer_ids), 1))
    paid = outcomes.filter(pl.col("paid_date").is_not_null())
    for i, b in enumerate(buyer_ids):
        wd = paid.filter(pl.col("buyer_id") == b)["paid_date"].dt.weekday().to_numpy() - 1  # Monday = 0
        if wd.size >= MIN_WEEKDAY_PAYMENTS:
            counts = np.bincount(wd, minlength=7) + 0.5
            out[i] = counts / counts.sum()
    return out


def gst_known(invoices: pl.DataFrame, purchases: pl.DataFrame, gst_rate: float, first_base_month: int):
    """Known monthly nets (taxable sales - taxable purchases) and the carry into first_base_month."""
    net: dict[int, float] = {}
    for d, t in zip(invoices["issue_date"], invoices["taxable"]):
        net[month_key(d)] = net.get(month_key(d), 0.0) + t
    for d, a in zip(purchases["value_date"], purchases["amount_paise"]):
        net[month_key(d)] = net.get(month_key(d), 0.0) - abs(a) / (1 + gst_rate)
    carry = 0.0
    for m in sorted(k for k in net if k < first_base_month):
        carry = min(gst_rate * net[m] + carry, 0.0)
    return {m: v for m, v in net.items() if m >= first_base_month}, carry


def bootstrap_unscheduled(txns: pl.DataFrame, categories: pl.DataFrame, as_of: date, full: bool = False):
    start = txns["value_date"].min() if full else as_of - timedelta(days=7 * BOOTSTRAP_WEEKS - 1)
    u = txns.filter(pl.col("value_date").is_between(start, as_of)).join(
        categories.select("txn_uid", "category"), on="txn_uid").filter(pl.col("category").is_in(UNSCHEDULED))
    days = pl.DataFrame({"value_date": pl.date_range(start, as_of, "1d", eager=True)})
    daily = days.join(u.group_by("value_date").agg(pl.col("amount_paise").sum()), on="value_date", how="left").fill_null(0)
    by_wd = {w: daily.filter(pl.col("value_date").dt.weekday() - 1 == w)["amount_paise"].to_numpy() for w in range(7)}

    def sample(n_paths: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
        out = np.zeros((n_paths, horizon + 1), dtype=np.int64)
        for d in range(1, horizon + 1):
            pool = by_wd[(as_of + timedelta(days=d)).weekday()]
            out[:, d] = pool[rng.integers(0, pool.size, n_paths)]
        return out

    return sample


def build_learned_inputs(obs: dict[str, pl.DataFrame], categories: pl.DataFrame, as_of: date, run_id: str,
                         threshold_paise: int = 0, gst_rate: float = 0.18, horizon: int = 90,
                         with_sales: bool = True, label_lag_days: int = 3, sales_uncertainty: bool = True,
                         sector_shocks: bool = True, demand_ar1: bool = True, full_unscheduled: bool = True,
                         naive_purchases: bool = False, timing_priors: dict | None = None,
                         timing_hierarchical: bool = True) -> EngineInputs:
    inv, out, buyers, txns = obs["invoices"], obs["invoice_outcomes"], obs["buyers"], obs["transactions"]
    tenant = inv["tenant_id"][0]
    buyer_ids = buyers["buyer_id"].to_list()
    bidx = {b: i for i, b in enumerate(buyer_ids)}

    tm = fit_timing(out, inv, buyers, as_of, run_id=run_id, label_lag_days=label_lag_days,
                    type_defaults=timing_priors, hierarchical=timing_hierarchical)
    timing = TimingDraws(p_early=[tm.posteriors[b].p_early for b in buyer_ids],
                         p=[tm.posteriors[b].p for b in buyer_ids], pi=[tm.posteriors[b].pi for b in buyer_ids])

    open_ = inv.filter(pl.col("status") != "paid").join(out.select("invoice_id", "paid_fraction"), on="invoice_id")
    cash = ((open_["gross_paise"] - open_["expected_tds_paise"]) * (1 - open_["paid_fraction"])).round().cast(pl.Int64)
    open_inv = Invoices(
        buyer=np.array([bidx[b] for b in open_["buyer_id"]], dtype=np.int64),
        issue_day=np.array([(d - as_of).days for d in open_["issue_date"]], dtype=np.int64),
        due_week=(open_["terms_days"].to_numpy() // 7 + 1).astype(np.int64),
        cash=cash.to_numpy(),
        gross=open_["gross_paise"].to_numpy(),
        taxable=(open_["gross_paise"] - open_["gst_paise"]).to_numpy().astype(float),
    )

    sales_fn = None
    if with_sales:
        sm = fit_sales(inv, txns, categories, as_of)

        def sales_fn(n_paths: int, H: int, rng: np.random.Generator):
            if demand_ar1:
                fut, pur = sm.sample(n_paths, H, rng, param_draws=sales_uncertainty, demand="ar1")
            else:
                fut, pur = sm.sample(n_paths, H, rng, level=sm.demand_levels(n_paths, rng),
                                     param_draws=sales_uncertainty)
            b = np.array([bidx[x] for x in fut["buyer_id"]], dtype=np.int64)
            gross = fut["gross_paise"].to_numpy()
            tds = np.round(gross * np.array([sm.tds_ratio.get(x, 0.0) for x in fut["buyer_id"]])).astype(np.int64)
            invoices = Invoices(
                buyer=b, issue_day=np.array([(d - as_of).days for d in fut["issue_date"]], dtype=np.int64),
                due_week=(fut["terms_days"].to_numpy() // 7 + 1).astype(np.int64), cash=gross - tds, gross=gross,
                taxable=gross / (1 + gst_rate), path=fut["path"].to_numpy().astype(np.int64))
            purchases = pur.select("path", ((pl.col("value_date") - pl.lit(as_of)).dt.total_days()).alias("day"),
                                   "amount_paise")
            return invoices, purchases

    obligations = detect_obligations(txns, categories, as_of, horizon)
    fixed = [((d - as_of).days, int(a)) for d, a in zip(obligations["due_date"], obligations["amount_paise"])
             if a is not None]
    if naive_purchases and not with_sales:
        sp = txns.join(categories.select("txn_uid", "category"), on="txn_uid").filter(
            (pl.col("category") == "supplier_payment") & (pl.col("value_date") <= as_of))
        span = (as_of - txns["value_date"].min()).days + 1
        daily = int(round(abs(sp["amount_paise"].sum()) / span))
        fixed += [(d, daily) for d in range(1, horizon + 1)]

    shock_sampler = None
    if sector_shocks:
        means = {b: (float(tm.posteriors[b].p_early.mean()), float(tm.posteriors[b].p.mean()),
                     float(tm.posteriors[b].pi.mean())) for b in buyer_ids}
        sector_of = dict(zip(buyers["buyer_id"], buyers["sector"]))
        shock_sampler = fit_sector_shocks(tm.labels, means, sector_of, as_of).shift_sampler(
            [sector_of[b] for b in buyer_ids])
    gst_days = [(d - as_of).days for d, k in zip(obligations["due_date"], obligations["kind"]) if k == "gst"]
    first_base = month_key(as_of + timedelta(days=min(gst_days))) - 1 if gst_days else month_key(as_of)
    supplier = txns.join(categories.select("txn_uid", "category"), on="txn_uid").filter(
        (pl.col("category") == "supplier_payment") & (pl.col("value_date") <= as_of))
    known, carry = gst_known(inv.with_columns((pl.col("gross_paise") - pl.col("gst_paise")).alias("taxable")),
                             supplier, gst_rate, first_base)

    bal = obs["balances"].filter(pl.col("date") <= as_of).sort("date")
    return EngineInputs(
        tenant_id=tenant, as_of=as_of, opening_balance_paise=int(bal["closing_paise"][-1]),
        threshold_paise=threshold_paise, buyer_ids=buyer_ids, timing=timing,
        weekday_probs=weekday_probs(out, buyer_ids), open_invoices=open_inv,
        survival_through_day=-label_lag_days, sales=sales_fn, fixed=fixed, gst_rate=gst_rate,
        gst_due_days=gst_days, gst_history_net=known, gst_carry_in=carry,
        unscheduled=bootstrap_unscheduled(txns, categories, as_of, full=full_unscheduled), shift_sampler=shock_sampler,
        model_versions={"payment_timing": "beta-geometric-2phase-v1", "sales_forecaster": "negbin-v1",
                        "obligations_detector": "v1", "unscheduled": "bootstrap-8w-v0"},
        input_snapshot_ids=[f"{k}:{_snapshot_id(v)}" for k, v in sorted(obs.items())],
    )
