"""True-parameter mode: engine inputs built from a synthetic world's Truth.

Every component follows the generator's own future process, so the engine's
p_short must match Futures.oracle_shortfall up to Monte Carlo error. Used only
to test the engine mechanics, never for a real tenant.

Mirrors the generator: true p_early/p/pi/loading per buyer; sector shock and
demand AR(1)s continued from their values in the week containing as_of;
orders in that week kept only after as_of; pipeline invoices (ordered on or
before as_of, issued after) fixed; true fixed obligations; GST from true
history plus each path's sales; the generator's unscheduled flows.
Requires part_payment_prob = 0, month_end_clustering off, geometric lateness,
no behaviour change and demand_shock_corr = 0 (not mirrored).
"""

import calendar
import math
from datetime import timedelta

import numpy as np
import polars as pl

from matsya_intelligence.engine.core import EngineInputs, Invoices, TimingDraws, month_key
from matsya_intelligence.engine.learned import gst_known
from matsya_intelligence.synth.world import Truth


def build_true_inputs(truth: Truth, n_paths: int, rng_shocks: np.random.Generator, horizon: int = 90) -> EngineInputs:
    c = truth.config
    assert c.part_payment_prob == 0 and not c.month_end_clustering and c.lateness == "geometric"
    assert c.behaviour_change is None and c.demand_shock_corr == 0
    t0 = c.as_of_date
    buyers = truth.buyers
    buyer_ids = buyers["buyer_id"].to_list()
    bidx = {b: i for i, b in enumerate(buyer_ids)}
    sector_of = np.array([c.sectors.index(s) for s in buyers["sector"]])
    loading = buyers["loading"].to_numpy()
    w0 = (t0 - c.start).days // 7  # world week containing as_of
    offset = (t0 - c.start).days  # world day of as_of

    # Sector shocks: known through w0, simulated per path after.
    n_world = (horizon + 7 * 60) // 7 + w0 + 2
    z_known = truth.sector_shocks.pivot(on="sector", index="week", values="z").sort("week")
    z_hist = np.stack([z_known[s].to_numpy() for s in c.sectors])  # (sectors, weeks)
    z = np.empty((n_paths, len(c.sectors), n_world))
    z[:, :, : w0 + 1] = z_hist[None, :, : w0 + 1]
    for w in range(w0 + 1, n_world):
        z[:, :, w] = c.shock_rho * z[:, :, w - 1] + c.shock_sigma * rng_shocks.standard_normal((n_paths, len(c.sectors)))

    def shift(path, buyer, week_start_day):
        ww = np.clip((week_start_day + offset) // 7, 0, n_world - 1)
        return loading[buyer] * z[path, sector_of[buyer], ww]

    timing = TimingDraws(p_early=[np.array([x]) for x in buyers["p_early"]], p=[np.array([x]) for x in buyers["p"]],
                         pi=[np.array([x]) for x in buyers["pi"]])

    inv = truth.invoices
    purchases = truth.purchases
    pipeline_ids = purchases.filter(pl.col("value_date") <= t0)["invoice_id"]
    open_ = inv.filter(
        ((pl.col("issue_date") <= t0) & (pl.col("first_paid_date").is_null() | (pl.col("first_paid_date") > t0)))
        | ((pl.col("issue_date") > t0) & pl.col("invoice_id").is_in(pipeline_ids.implode())))

    def as_invoices(df: pl.DataFrame) -> Invoices:
        return Invoices(
            buyer=np.array([bidx[b] for b in df["buyer_id"]], dtype=np.int64),
            issue_day=np.array([(d - t0).days for d in df["issue_date"]], dtype=np.int64),
            due_week=(df["terms_days"].to_numpy() // 7 + 1).astype(np.int64),
            cash=(df["gross_paise"] - df["tds_paise"]).to_numpy(), gross=df["gross_paise"].to_numpy(),
            taxable=(df["gross_paise"] - df["gst_paise"]).to_numpy().astype(float))

    mix = buyers["mix_share"].to_numpy()
    tds_rate = buyers["tds_rate"].to_numpy()
    var_x = c.demand_sigma**2 / (1 - c.demand_rho**2)
    x_w0 = float(truth.weekly_orders.filter(pl.col("week") == w0)["demand_x"][0])

    def sales_fn(n: int, H: int, rng: np.random.Generator):
        last_day = H  # orders after H produce nothing inside the horizon
        n_weeks = (last_day + offset) // 7 - w0 + 1
        x = np.full(n, x_w0)
        rows = []
        for j in range(n_weeks):
            w = w0 + j
            if j > 0:
                x = c.demand_rho * x + c.demand_sigma * rng.standard_normal(n)
            mean = c.order_mean_per_week * np.exp(x - var_x / 2)
            counts = rng.poisson(rng.gamma(c.order_dispersion, mean / c.order_dispersion))
            path = np.repeat(np.arange(n), counts)
            m = path.size
            buyer = rng.choice(len(buyer_ids), size=m, p=mix)
            day = 7 * w - offset + rng.integers(0, 7, m)
            gross = np.round(c.amount_median_paise * np.exp(c.amount_sigma * rng.standard_normal(m))).astype(np.int64)
            terms = rng.choice(np.array(c.terms_choices), size=m)
            lag = c.purchase_lag_days + rng.integers(-c.purchase_lag_jitter_days, c.purchase_lag_jitter_days + 1, m)
            keep = (day >= 1) & (day <= last_day)
            rows.append((path[keep], buyer[keep], day[keep], gross[keep], terms[keep], lag[keep]))
        path, buyer, day, gross, terms, lag = (np.concatenate(z_) for z_ in zip(*rows))
        gst = np.round(gross * c.gst_rate / (1 + c.gst_rate)).astype(np.int64)
        tds = np.round(tds_rate[buyer] * (gross - gst)).astype(np.int64)
        issue = day + lag
        sel = issue <= H
        invoices = Invoices(buyer=buyer[sel], issue_day=issue[sel], due_week=terms[sel] // 7 + 1,
                            cash=(gross - tds)[sel], gross=gross[sel], taxable=(gross - gst)[sel].astype(float),
                            path=path[sel])
        purch = pl.DataFrame({"path": path, "day": day,
                              "amount_paise": np.round(c.purchase_ratio * gross).astype(np.int64)})
        return invoices, purch

    obl = truth.obligations.filter(pl.col("due_date").is_between(t0 + timedelta(days=1), t0 + timedelta(days=horizon)))
    fixed = [((d - t0).days, int(a)) for d, a, k in zip(obl["due_date"], obl["amount_paise"], obl["kind"]) if k != "gst"]
    gst_days = [(d - t0).days for d, k in zip(obl["due_date"], obl["kind"]) if k == "gst"]
    first_base = month_key(t0 + timedelta(days=min(gst_days))) - 1 if gst_days else month_key(t0)
    known_inv = inv.filter((pl.col("issue_date") <= t0) | pl.col("invoice_id").is_in(pipeline_ids.implode()))
    known, carry = gst_known(known_inv.with_columns((pl.col("gross_paise") - pl.col("gst_paise")).alias("taxable")),
                             purchases.filter(pl.col("value_date") <= t0), c.gst_rate, first_base)

    def unscheduled(n: int, H: int, rng: np.random.Generator) -> np.ndarray:
        out = np.zeros((n, H + 1), dtype=np.int64)
        for d in range(1, H + 1):
            day = t0 + timedelta(days=d)
            k = rng.poisson(c.misc_credits_per_day, n)
            amt = np.round(c.misc_credit_median_paise * np.exp(0.6 * rng.standard_normal(k.sum()))).astype(np.int64)
            np.add.at(out[:, d], np.repeat(np.arange(n), k), amt)
            k = rng.poisson(c.misc_expenses_per_day, n)
            amt = np.round(c.misc_expense_median_paise * np.exp(0.6 * rng.standard_normal(k.sum()))).astype(np.int64)
            np.subtract.at(out[:, d], np.repeat(np.arange(n), k), amt)
            cash = rng.random(n) < c.cash_withdrawals_per_week / 7
            out[:, d] -= np.where(cash, rng.integers(1, 6, n) * 10_000_00, 0)
            own = rng.random(n) < c.owner_transfers_per_month / 30
            out[:, d] -= np.where(own, rng.integers(5, 21, n) * 10_000_00, 0)
            if day.day == calendar.monthrange(day.year, day.month)[1]:
                out[:, d] -= rng.integers(59, 591, n) * 100
            if day.day == 1 and day.month in (1, 4, 7, 10):
                out[:, d] -= rng.integers(15, 60, n) * 100
        return out

    return EngineInputs(
        tenant_id=c.tenant_id, as_of=t0, opening_balance_paise=c.as_of_balance_paise,
        threshold_paise=c.shortfall_threshold_paise, buyer_ids=buyer_ids, timing=timing,
        weekday_probs=np.full((len(buyer_ids), 7), 1 / 7), open_invoices=as_invoices(open_),
        survival_through_day=0, sales=sales_fn, fixed=fixed, gst_rate=c.gst_rate, gst_due_days=gst_days,
        gst_history_net=known, gst_carry_in=carry, unscheduled=unscheduled, shift=shift,
        model_versions={"mode": "true-parameter"}, input_snapshot_ids=[f"synth:{truth.past_seed}"],
    )
