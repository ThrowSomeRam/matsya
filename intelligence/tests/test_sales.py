from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from matsya_contracts.validate import validate
from matsya_intelligence.sales import fit_sales, gst_by_month
from matsya_intelligence.io.adapters import track_b_inputs
from matsya_intelligence.synth import WorldConfig, generate_world

# Weeks aligned with the generator's order weeks and a whole-week lag, so weekly
# invoice counts are exactly the planted negative binomial (see test docstring).
ALIGNED = dict(start=date(2026, 3, 31) - timedelta(weeks=570), demand_sigma=0.0,
               purchase_lag_days=14, purchase_lag_jitter_days=0)


def _fit(cfg: WorldConfig, seed: int):
    tables, truth = generate_world(cfg, past_seed=seed, future_seed=0)
    obs = track_b_inputs(tables, cfg.as_of)
    return fit_sales(obs["invoices"], obs["transactions"], truth.transactions.select("txn_uid", "category"),
                     cfg.as_of_date), truth


def test_recovers_planted_negative_binomial():
    """With the default lag jitter, an order week's invoices straddle two issue
    weeks, which smooths counts and biases dispersion upward; aligned worlds
    remove that, so the planted values must come back."""
    cfg = WorldConfig(**ALIGNED)
    fits = [_fit(cfg, s)[0] for s in range(4)]
    assert abs(np.mean([f.mean_per_week for f in fits]) - cfg.order_mean_per_week) < 0.05 * cfg.order_mean_per_week
    assert abs(np.mean([f.dispersion for f in fits]) - cfg.order_dispersion) < 0.2 * cfg.order_dispersion
    for f in fits:
        assert abs(f.purchase_ratio - cfg.purchase_ratio) < 0.01
        assert f.purchase_lag_days == 14


def test_recovers_ratio_and_lag_default_world():
    cfg = WorldConfig()
    for s in range(3):
        f, _ = _fit(cfg, s)
        assert abs(f.purchase_ratio - cfg.purchase_ratio) < 0.015
        assert abs(f.purchase_lag_days - cfg.purchase_lag_days) <= 1
        validate(f.contract_features(), "features.sales_process")
        assert abs(f.mix.sum() - 1) < 1e-12


def test_sampled_futures_match_fitted_rates():
    f, _ = _fit(WorldConfig(**ALIGNED), 0)
    H = 91
    inv, pur = f.sample(4000, H, np.random.default_rng(1))
    weekly = inv.height / 4000 / (H / 7)
    assert abs(weekly - f.mean_per_week) < 0.03 * f.mean_per_week
    per_path = inv.group_by("path").len()["len"].to_numpy()
    # Over 13 weeks the count variance is 13 (m + m^2 / k) for independent weeks.
    var_expected = 13 * (f.mean_per_week + f.mean_per_week**2 / f.dispersion)
    assert abs(per_path.var() / var_expected - 1) < 0.15
    a = f.as_of
    assert inv["issue_date"].min() > a and inv["issue_date"].max() <= a + timedelta(days=H)
    assert pur["value_date"].min() > a and pur["value_date"].max() <= a + timedelta(days=H)
    # Purchases on day d belong to invoices on day d + L: compare daily means away from the edges.
    mean_inv = inv["gross_paise"].sum() / 4000 / H
    mean_pur = pur["amount_paise"].sum() / 4000 / H
    assert abs(mean_pur / mean_inv - f.purchase_ratio) < 0.02
    b_share = inv.group_by("buyer_id").len().with_columns(pl.col("len") / inv.height)
    for b, s in zip(b_share["buyer_id"], b_share["len"]):
        assert abs(s - f.mix[f.buyer_ids.index(b)]) < 0.01


def test_sampling_reproducible():
    f, _ = _fit(WorldConfig(), 0)
    a = f.sample(50, 30, np.random.default_rng(5))
    b = f.sample(50, 30, np.random.default_rng(5))
    assert a[0].equals(b[0]) and a[1].equals(b[1])


def test_gst_matches_generator():
    _, truth = _fit(WorldConfig(), 2)
    g = gst_by_month(truth.invoices, truth.purchases, truth.config.gst_rate)
    paid = truth.obligations.filter(pl.col("kind") == "gst").with_columns(
        (pl.col("due_date").dt.truncate("1mo") - pl.duration(days=1)).dt.truncate("1mo").alias("month"))
    j = paid.join(g, on="month").sort("month")[1:]  # first month: pre-roll invoices are outside truth.invoices
    assert j.height >= 10
    assert ((j["amount_paise"] - j["gst_paise"]).abs() <= 100).all()


def test_parameter_draws_widen_path_totals():
    f, _ = _fit(WorldConfig(), 0)
    assert f.boot_mean.size == f.boot_ratio.size == 200
    assert 0 < f.boot_mean.std() < 0.2 * f.mean_per_week
    inv_point, _ = f.sample(3000, 91, np.random.default_rng(2))
    inv_boot, _ = f.sample(3000, 91, np.random.default_rng(2), param_draws=True)
    # Same mean rate, wider spread of per-path totals once parameter uncertainty is in.
    a = inv_point.group_by("path").len()["len"].to_numpy()
    b = inv_boot.group_by("path").len()["len"].to_numpy()
    assert abs(b.mean() / a.mean() - 1) < 0.05
    assert b.var() > a.var()


def test_block_bootstrap_widens_mean_spread_under_autocorrelation():
    from matsya_intelligence.sales.model import _block_indices

    rng = np.random.default_rng(0)
    x = np.zeros(520)
    for t in range(1, x.size):
        x[t] = 0.9 * x[t - 1] + rng.standard_normal()
    idx_block = _block_indices(np.random.default_rng(1), x.size, 4, 2000)
    idx_iid = np.random.default_rng(1).integers(0, x.size, (2000, x.size))
    assert idx_block.shape == (2000, x.size)
    assert x[idx_block].mean(axis=1).std() > 1.5 * x[idx_iid].mean(axis=1).std()
