from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from matsya_contracts.validate import ContractError, validate

from matsya_intelligence.io import adapters
from matsya_intelligence.io.types import READS
from matsya_intelligence.synth import PRESETS, BehaviourChange, TypePrior, WorldConfig, generate_world, resample_futures
from matsya_intelligence.synth.world import PROCESS_OF_CATEGORY
from matsya_intelligence.synth.narration import gstin_checksum

AS_OF_TABLES = ("ledger.transactions", "ledger.balances", "ledger.invoices", "ledger.matches", "labels.invoice_outcomes")


@pytest.fixture(scope="module")
def world():
    return generate_world(WorldConfig(), past_seed=7, future_seed=0)


def test_observed_tables_validate(world):
    observed, truth = world
    assert set(observed) == set(READS)
    for name, df in observed.items():
        if name != "labels.txn_corrections":
            assert df.height > 0, name
        validate(df, name)
    validate(truth.obligations, "features.obligations")
    validate(truth.transactions.drop("category", "rail", "process"), "ledger.transactions")


def test_validate_rejects_bad_sign(world):
    txns = world[0]["ledger.transactions"].with_columns(pl.col("amount_paise").abs())
    with pytest.raises(ContractError):
        validate(txns, "ledger.transactions")


def test_same_seed_identical():
    a_obs, a_truth = generate_world(WorldConfig(), past_seed=3, future_seed=0)
    b_obs, b_truth = generate_world(WorldConfig(), past_seed=3, future_seed=0)
    for key in a_obs:
        assert a_obs[key].equals(b_obs[key]), key
    assert a_truth.transactions.equals(b_truth.transactions)
    assert a_truth.invoices.equals(b_truth.invoices)
    c_obs, _ = generate_world(WorldConfig(), past_seed=4, future_seed=0)
    assert not a_obs["ledger.transactions"].equals(c_obs["ledger.transactions"])


def test_no_look_ahead(world):
    observed, truth = world
    as_of = truth.config.as_of
    for name in AS_OF_TABLES:
        assert observed[name]["known_at"].max() <= as_of, name
    assert observed["ledger.balances"]["date"].max() == truth.config.as_of_date
    assert observed["ledger.invoices"]["issue_date"].max() <= truth.config.as_of_date
    assert observed["labels.invoice_outcomes"]["paid_date"].drop_nulls().max() <= truth.config.as_of_date
    # Truth does hold the future.
    assert truth.transactions["value_date"].max() > truth.config.as_of_date


def test_open_invoices_are_censored(world):
    observed, truth = world
    out = adapters.outcomes(observed, truth.config.as_of)
    assert out.filter(pl.col("paid_date").is_null() & pl.col("censored_at").is_null()).height == 0
    assert out.filter(pl.col("paid_date").is_not_null() & pl.col("censored_at").is_not_null()).height == 0
    assert (out.filter(pl.col("censored_at").is_not_null())["censored_at"] == truth.config.as_of_date).all()


@pytest.mark.parametrize("which", ["observed", "truth"])
def test_balances_reconcile(world, which):
    observed, truth = world
    if which == "observed":
        txns = observed["ledger.transactions"]
        bal = observed["ledger.balances"].filter(pl.col("balance_source") == "reconstructed").sort("date")
    else:
        txns, bal = truth.transactions, truth.balances
    opening = truth.opening_balance_paise
    assert bal["closing_paise"][0] == opening
    assert bal["closing_paise"][-1] == opening + txns["amount_paise"].sum()
    # Day by day: each closing equals the previous closing plus that day's flows.
    daily = txns.group_by("value_date").agg(pl.col("amount_paise").sum())
    joined = bal.join(daily, left_on="date", right_on="value_date", how="left").fill_null(0).sort("date")
    diffs = joined["closing_paise"].diff().drop_nulls()
    assert (diffs == joined["amount_paise"][1:]).all()


def test_bank_reported_balance_is_true_balance(world):
    observed, truth = world
    _, bal, src = adapters.opening_balance(observed, truth.config.as_of)
    assert src == "bank_reported" and bal == truth.config.as_of_balance_paise


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
        pre_due_phase=False,  # these checks use single-phase geometric moments
        **overrides,
    )
    return generate_world(cfg, past_seed=11, future_seed=0)[1]


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
        return truth.invoices.filter((pl.col("buyer_id") == truth.buyers["buyer_id"][0]) & (pl.col("issue_date") > at))[
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
    return generate_world(cfg, past_seed=5, future_seed=0)[1]


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
        truth = generate_world(WorldConfig(order_mean_per_week=order_mean), past_seed=9, future_seed=0)[1]
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
    lag = pl.duration(days=truth.config.purchase_lag_days)
    sales = truth.invoices.select((pl.col("issue_date") - lag).alias("d"), "gross_paise").group_by(month).agg(
        pl.col("gross_paise").sum())
    purch = truth.transactions.filter(pl.col("category") == "supplier_payment").select(
        pl.col("value_date").alias("d"), (-pl.col("amount_paise")).alias("p")).group_by(month).agg(pl.col("p").sum())
    m = sales.join(purch, on="d").sort("d")[1:-1]  # drop partial first and last months
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
        truth = generate_world(cfg, past_seed=2, future_seed=0)[1]
        x = truth.weekly_orders["demand_x"].to_numpy()
        e_d = x[1:] - cfg.demand_rho * x[:-1]
        z = truth.sector_shocks.pivot(on="sector", index="week", values="z").drop("week").to_numpy().T
        e_z = (z[:, 1:] - cfg.shock_rho * z[:, :-1]).mean(axis=0)[: len(e_d)]
        return float(np.corrcoef(e_d, e_z)[0, 1])

    assert abs(innovation_corr(0.0)) < 0.1
    assert innovation_corr(-0.8) < -0.7


@pytest.mark.parametrize("name,lo,hi", [("healthy", 0.0, 0.10), ("borderline", 0.20, 0.40), ("distressed", 0.70, 1.0)])
def test_presets_land_in_shortfall_band(name, lo, hi):
    """Median over fixed pasts of the oracle P(short by day 90). See PRESETS for the spread."""
    cfg = WorldConfig.preset(name)
    p90 = [resample_futures(cfg, past_seed=ps, n=40).oracle_shortfall()["p_short"][-1] for ps in range(7)]
    assert lo <= float(np.median(p90)) <= hi


def test_default_is_borderline():
    assert WorldConfig() == WorldConfig.preset("borderline")
    assert set(PRESETS) == {"healthy", "borderline", "distressed"}


def _ipc_bytes(df: pl.DataFrame) -> bytes:
    return df.write_ipc(None).getvalue()


def test_past_identical_across_future_seeds():
    a_obs, a_truth = generate_world(WorldConfig(), past_seed=7, future_seed=0)
    b_obs, b_truth = generate_world(WorldConfig(), past_seed=7, future_seed=1)
    for key in a_obs:
        assert _ipc_bytes(a_obs[key]) == _ipc_bytes(b_obs[key]), key
    t0 = a_truth.config.as_of_date
    past = lambda t: t.transactions.filter(pl.col("value_date") <= t0)
    # A filtered slice shares string buffers with future rows, so compare a
    # canonical serialisation rather than raw Arrow buffers.
    assert past(a_truth).write_csv() == past(b_truth).write_csv()
    assert a_truth.opening_balance_paise == b_truth.opening_balance_paise
    # The future does differ.
    assert not a_truth.transactions.equals(b_truth.transactions)


def test_open_invoices_resampled_paid_ones_fixed():
    futures = resample_futures(WorldConfig(), past_seed=7, n=12)
    t0 = futures.config.as_of_date
    # Truly unpaid at as_of (the observed tables also show payments in the
    # known_at lag window as open, so take this from the truth).
    t = futures.truths[0].invoices
    open_ids = t.filter((pl.col("issue_date") <= t0) & (pl.col("first_paid_date").is_null() | (pl.col("first_paid_date") > t0)))["invoice_id"]
    first = [t.invoices.filter(pl.col("invoice_id").is_in(open_ids.implode()))["first_paid_date"] for t in futures.truths]
    for dates in first:
        assert (dates.drop_nulls() > t0).all()
    # Payment dates of open invoices vary across futures.
    assert len({tuple(d.to_list()) for d in first}) > 1
    paid_past = [t.invoices.filter(pl.col("first_paid_date") <= t0).select("invoice_id", "first_paid_date")
                 for t in futures.truths]
    assert all(p.equals(paid_past[0]) for p in paid_past)


def test_conditional_timing_matches_unconditional():
    """Invoices issued in the 8 weeks before as_of are mostly open at as_of, so
    their timing comes from the conditional redraw. Pooled over pasts it must
    still match the planted geometric: E[pay_week * p] = 1, never share = pi."""
    cfg = WorldConfig(shock_sigma=0.0, n_buyers=1, demand_sigma=0.0, order_mean_per_week=30.0, pre_due_phase=False,
                      type_priors={"listed": TypePrior(p_ab=(300, 700), pi_ab=(100, 900), share=1.0)})
    t0 = cfg.as_of_date
    scaled_weeks, never_gap, n = [], [], 0
    for ps in range(10):
        truth = generate_world(cfg, past_seed=ps, future_seed=0)[1]
        p, pi = truth.buyers["p"][0], truth.buyers["pi"][0]
        inv = truth.invoices.filter(pl.col("issue_date").is_between(t0 - timedelta(days=56), t0 - timedelta(days=7)))
        scaled_weeks.extend((inv["pay_week"].drop_nulls() * p).to_list())
        never_gap.extend((inv["never_paid"].cast(pl.Float64) - pi).to_list())
    assert len(scaled_weeks) > 1500
    assert abs(np.mean(scaled_weeks) - 1) < 0.08
    assert abs(np.mean(never_gap)) < 0.03


def test_as_of_balance_pinned():
    for ps in range(3):
        truth = generate_world(WorldConfig(), past_seed=ps, future_seed=0)[1]
        bal = truth.balances.filter(pl.col("date") == truth.config.as_of_date)["closing_paise"][0]
        assert bal == truth.config.as_of_balance_paise


def test_gst_is_rate_times_sales_minus_purchases(world):
    truth = world[1]
    c = truth.config
    month = lambda col: pl.col(col).dt.truncate("1mo").alias("m")
    sales = truth.invoices.group_by(month("issue_date")).agg((pl.col("gross_paise") - pl.col("gst_paise")).sum().alias("s"))
    purch = truth.purchases.group_by(month("value_date")).agg((pl.col("amount_paise") / (1 + c.gst_rate)).sum().alias("p"))
    gst = truth.obligations.filter(pl.col("kind") == "gst").with_columns(
        (pl.col("due_date").dt.truncate("1mo") - pl.duration(days=1)).dt.truncate("1mo").alias("m"))
    j = gst.join(sales, on="m").join(purch, on="m")
    assert j.height >= 6
    expected = (c.gst_rate * (j["s"] - j["p"])).round()
    # Within a few rupees: invoices issued before start are not in truth.invoices.
    assert ((j["amount_paise"] - expected).abs() <= 100).all()


def test_no_missing_purchases_at_window_end(world):
    truth = world[1]
    end = truth.config.as_of_date + timedelta(days=truth.config.future_days)
    last_weeks = truth.transactions.filter(
        (pl.col("category") == "supplier_payment") & (pl.col("value_date") > end - timedelta(days=21)))
    earlier = truth.transactions.filter(
        (pl.col("category") == "supplier_payment") & pl.col("value_date").is_between(end - timedelta(days=63), end - timedelta(days=21), closed="left"))
    # Purchase rate in the last 3 weeks is comparable to the 6 weeks before.
    assert last_weeks.height / 3 > 0.5 * earlier.height / 6


def test_oracle_shortfall_monotone_and_threshold():
    futures = resample_futures(WorldConfig(), past_seed=0, n=20)
    base = futures.oracle_shortfall(horizon_days=90)
    assert base.height == 90
    assert (base["p_short"].diff().drop_nulls() >= 0).all()
    higher = futures.oracle_shortfall(threshold_paise=10_00_000_00, horizon_days=90)
    assert (higher["p_short"] >= base["p_short"]).all()
    with pytest.raises(ValueError):
        futures.oracle_shortfall(horizon_days=futures.config.future_days + 1)
