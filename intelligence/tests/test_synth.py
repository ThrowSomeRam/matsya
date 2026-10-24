from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from matsya_intelligence.io.types import SchemaError, validate
from matsya_intelligence.synth import PRESETS, BehaviourChange, TypePrior, WorldConfig, generate_world
from matsya_intelligence.synth.world import PROCESS_OF_CATEGORY
from matsya_intelligence.synth.narration import gstin_checksum

OBSERVED_SCHEMA = {
    "transactions": "Txn",
    "balances": "Balance",
    "invoices": "Invoice",
    "invoice_outcomes": "InvoiceOutcome",
    "buyers": "Buyer",
}


@pytest.fixture(scope="module")
def world():
    return generate_world(WorldConfig(), seed=7)


def test_observed_tables_validate(world):
    observed, truth = world
    assert set(observed) == set(OBSERVED_SCHEMA)
    for key, name in OBSERVED_SCHEMA.items():
        assert observed[key].height > 0, key
        validate(observed[key], name)
    validate(truth.obligations, "Obligation")
    validate(truth.balances, "Balance")
    validate(truth.transactions.drop("category", "rail", "process"), "Txn")


def test_validate_rejects_bad_sign(world):
    txns = world[0]["transactions"].with_columns(pl.col("amount_paise").abs())
    with pytest.raises(SchemaError):
        validate(txns, "Txn")


def test_same_seed_identical():
    a_obs, a_truth = generate_world(WorldConfig(), seed=3)
    b_obs, b_truth = generate_world(WorldConfig(), seed=3)
    for key in a_obs:
        assert a_obs[key].equals(b_obs[key]), key
    assert a_truth.transactions.equals(b_truth.transactions)
    assert a_truth.invoices.equals(b_truth.invoices)
    c_obs, _ = generate_world(WorldConfig(), seed=4)
    assert not a_obs["transactions"].equals(c_obs["transactions"])


def test_no_look_ahead(world):
    observed, truth = world
    as_of = truth.config.as_of
    assert observed["transactions"]["known_at"].max() <= as_of
    assert observed["balances"]["date"].max() == truth.config.as_of_date
    assert observed["invoices"]["issue_date"].max() <= truth.config.as_of_date
    assert observed["invoice_outcomes"]["paid_date"].drop_nulls().max() <= truth.config.as_of_date
    # Truth does hold the future.
    assert truth.transactions["value_date"].max() > truth.config.as_of_date


def test_open_invoices_are_censored(world):
    out = world[0]["invoice_outcomes"]
    assert out.filter(pl.col("paid_date").is_null() & pl.col("censored_at").is_null()).height == 0
    assert out.filter(pl.col("paid_date").is_not_null() & pl.col("censored_at").is_not_null()).height == 0


@pytest.mark.parametrize("which", ["observed", "truth"])
def test_balances_reconcile(world, which):
    observed, truth = world
    txns = observed["transactions"] if which == "observed" else truth.transactions
    bal = observed["balances"] if which == "observed" else truth.balances
    opening = truth.config.opening_balance_paise
    assert bal["closing_paise"][0] == opening
    assert bal["closing_paise"][-1] == opening + txns["amount_paise"].sum()
    # Day by day: each closing equals the previous closing plus that day's flows.
    daily = txns.group_by("value_date").agg(pl.col("amount_paise").sum())
    joined = bal.join(daily, left_on="date", right_on="value_date", how="left").fill_null(0).sort("date")
    diffs = joined["closing_paise"].diff().drop_nulls()
    assert (diffs == joined["amount_paise"][1:]).all()


def test_receipts_net_of_tds(world):
    truth = world[1]
    paid = truth.payments.group_by("invoice_id").agg(pl.col("amount_paise").sum(), pl.col("tds_paise").sum())
    j = truth.invoices.join(paid, on="invoice_id")
    assert (j["amount_paise"] + j["tds_paise_right"] == j["gross_paise"]).all()


def test_gstin_checksum_known_value():
    # Published GSTIN example (27AAPFU0939F1ZV).
    assert gstin_checksum("27AAPFU0939F1Z") == "V"


def test_categories_cover_rails(world):
    truth = world[1]
    assert {"customer_receipt", "supplier_payment", "salary", "gst_payment", "tds_payment",
            "pf_esi", "emi", "rent", "bank_charges"} <= set(truth.transactions["category"])


def test_gst_never_due_on_sunday(world):
    gst = world[1].obligations.filter(pl.col("kind") == "gst")
    assert all(d.weekday() != 6 for d in gst["due_date"])


def _single_type_world(**overrides):
    """Many invoices, no shock, one buyer type, long tail after the last cohort used."""
    cfg = WorldConfig(
        start=date(2022, 1, 1),
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
        future_days=0,
        n_buyers=3,
        type_priors={"listed": TypePrior(p_ab=(30, 70), pi_ab=(10, 90), share=1.0)},
        buyer_mix_concentration=1000.0,  # near-equal shares
        order_mean_per_week=60.0,
        demand_sigma=0.0,
        shock_sigma=0.0,
        misc_credits_per_day=0.0,
        **overrides,
    )
    return generate_world(cfg, seed=11)[1]


def test_empirical_lateness_matches_planted():
    truth = _single_type_world()
    cutoff = truth.config.as_of_date.replace(year=truth.config.as_of_date.year - 1)
    inv = truth.invoices.filter(pl.col("issue_date") < cutoff)  # a year of follow-up
    stats = inv.group_by("buyer_id").agg(
        pl.len().alias("n"),
        pl.col("pay_week").is_null().mean().alias("never_hat"),
        pl.col("pay_week").drop_nulls().mean().alias("mean_week"),
    ).join(truth.buyers, on="buyer_id")
    for row in stats.iter_rows(named=True):
        assert row["n"] > 1500
        # Unpaid after a year is pi to within sampling error (p^52 tail is negligible).
        se_pi = (row["pi"] * (1 - row["pi"]) / row["n"]) ** 0.5
        assert abs(row["never_hat"] - row["pi"]) < 4 * se_pi + 0.005
        # Geometric mean is 1/p.
        assert abs(row["mean_week"] - 1 / row["p"]) < 0.1 / row["p"]


def test_behaviour_change_slows_buyer():
    at = date(2023, 7, 1)
    base = _single_type_world()
    changed = _single_type_world(behaviour_change=BehaviourChange(buyer_index=0, at=at, p_multiplier=0.3))

    def mean_week(truth):
        return truth.invoices.filter((pl.col("buyer_id") == "B000") & (pl.col("issue_date") > at))[
            "pay_week"].drop_nulls().mean()

    assert mean_week(changed) > 2 * mean_week(base)


def test_month_end_clustering():
    truth = _single_type_world(month_end_clustering=True)
    d = truth.invoices["first_paid_date"].drop_nulls()
    share = sum((x + timedelta(days=1)).day == 1 for x in d) / len(d)
    assert share > 0.6


def test_lognormal_lateness_runs():
    truth = _single_type_world(lateness="lognormal")
    assert truth.invoices["pay_week"].drop_nulls().min() >= 1


@pytest.fixture(scope="module")
def long_world():
    """20 years, no demand shock: enough weeks to recover the planted order process."""
    cfg = WorldConfig(
        start=date(2005, 1, 1),
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
        future_days=0,
        n_buyers=3,
        demand_sigma=0.0,
    )
    return generate_world(cfg, seed=5)[1]


def test_order_count_recovers_negative_binomial(long_world):
    c = long_world.config
    counts = long_world.weekly_orders["count"][:-1]  # last week may be partial
    m, v = counts.mean(), counts.var()
    assert abs(m - c.order_mean_per_week) < 0.05 * c.order_mean_per_week
    k_hat = m**2 / (v - m)  # method of moments: Var = m + m^2 / k
    assert abs(k_hat - c.order_dispersion) < 0.3 * c.order_dispersion
    # Plain Poisson would put v near m; the planted dispersion must show.
    assert v > 1.5 * m


def test_purchases_recover_ratio_and_lag(long_world):
    c = long_world.config
    j = long_world.purchases.filter(pl.col("txn_uid").is_not_null()).join(long_world.invoices, on="invoice_id")
    assert j.height > 5000
    ratio = j["amount_paise"].sum() / j["gross_paise"].sum()
    assert abs(ratio - c.purchase_ratio) < 1e-4
    lag = (j["issue_date"] - j["value_date"]).dt.total_days()
    assert (lag == j["lag_days"]).all()
    assert abs(lag.mean() - c.purchase_lag_days) < 0.1
    assert lag.min() >= c.purchase_lag_days - c.purchase_lag_jitter_days
    assert lag.max() <= c.purchase_lag_days + c.purchase_lag_jitter_days


def test_purchases_scale_with_sales_fixed_costs_do_not():
    fixed = ["salary", "rent", "emi", "professional_fee", "pf_esi"]

    def totals(order_mean):
        truth = generate_world(WorldConfig(order_mean_per_week=order_mean), seed=9)[1]
        by_cat = dict(truth.transactions.group_by("category").agg(pl.col("amount_paise").sum()).iter_rows())
        return by_cat, truth

    lo, _ = totals(8.0)
    hi, truth = totals(16.0)
    assert 1.6 < hi["supplier_payment"] / lo["supplier_payment"] < 2.4
    for cat in fixed:
        assert hi[cat] == lo[cat], cat
    # Linked month by month: purchases track invoices issued (not receipts,
    # which lag by the payment timing).
    month = pl.col("d").dt.truncate("1mo")
    sales = truth.invoices.select(pl.col("issue_date").alias("d"), "gross_paise").group_by(month).agg(
        pl.col("gross_paise").sum())
    purch = truth.transactions.filter(pl.col("category") == "supplier_payment").select(
        pl.col("value_date").alias("d"), (-pl.col("amount_paise")).alias("p")).group_by(month).agg(pl.col("p").sum())
    m = sales.join(purch, on="d")
    assert m.select(pl.corr("gross_paise", "p")).item() > 0.8


def test_every_flow_tagged_with_one_process(world):
    txns = world[1].transactions
    assert txns["process"].null_count() == 0
    assert set(txns["process"]) == {"sales", "purchase", "fixed_obligation", "unscheduled"}
    per_cat = txns.group_by("category").agg(pl.col("process").n_unique())
    assert (per_cat["process"] == 1).all()
    assert set(txns["category"]) <= set(PROCESS_OF_CATEGORY)


def test_demand_shock_correlation_knob():
    def innovation_corr(corr):
        cfg = WorldConfig(start=date(2015, 1, 1), demand_shock_corr=corr)
        truth = generate_world(cfg, seed=2)[1]
        x = truth.weekly_orders["demand_x"].to_numpy()
        e_d = x[1:] - cfg.demand_rho * x[:-1]
        z = truth.sector_shocks.pivot(on="sector", index="week", values="z").drop("week").to_numpy().T
        e_z = (z[:, 1:] - cfg.shock_rho * z[:, :-1]).mean(axis=0)[: len(e_d)]
        return float(np.corrcoef(e_d, e_z)[0, 1])

    assert abs(innovation_corr(0.0)) < 0.1
    assert innovation_corr(-0.8) < -0.7


@pytest.mark.parametrize("name,lo,hi", [("healthy", 0.0, 0.10), ("borderline", 0.20, 0.40), ("distressed", 0.80, 1.0)])
def test_presets_land_in_shortfall_band(name, lo, hi):
    cfg = WorldConfig.preset(name)
    a = cfg.as_of_date
    hits = 0
    seeds = range(60)
    for s in seeds:
        bal = generate_world(cfg, seed=s)[1].balances
        window = bal.filter((pl.col("date") > a) & (pl.col("date") <= a + timedelta(days=90)))
        hits += window["closing_paise"].min() < 0
    assert lo <= hits / len(seeds) <= hi


def test_default_is_borderline():
    assert WorldConfig() == WorldConfig.preset("borderline")
    assert set(PRESETS) == {"healthy", "borderline", "distressed"}
