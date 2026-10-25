import json
import time
from datetime import timedelta

import numpy as np
import pytest

from matsya_intelligence.engine import OneOffEvent, build_learned_inputs, run_engine
from matsya_intelligence.engine.evaluate import gap1, learned_vs_oracle, true_vs_oracle
from matsya_intelligence.synth import WorldConfig, generate_world

N = 2000


@pytest.fixture(scope="module")
def learned():
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    cats = truth.transactions.select("txn_uid", "category")
    return cfg, obs, cats, build_learned_inputs(obs, cats, cfg.as_of_date, "t-run")


def test_conservation_every_path(learned):
    _, _, _, inp = learned
    res = run_engine(inp, "t-run", N)
    total = sum(res.component_totals.values())
    assert (res.balance[:, 0] == inp.opening_balance_paise).all()
    assert (res.balance[:, -1] == inp.opening_balance_paise + total).all()
    assert res.balance.dtype == np.int64
    assert {"open_invoice_receipts", "future_invoice_receipts", "purchases", "gst", "fixed_obligations",
            "unscheduled"} <= set(res.component_totals)


def test_replay_bit_identical(learned):
    cfg, obs, cats, _ = learned
    a = run_engine(build_learned_inputs(obs, cats, cfg.as_of_date, "replay"), "replay", N)
    b = run_engine(build_learned_inputs(obs, cats, cfg.as_of_date, "replay"), "replay", N)
    assert np.array_equal(a.balance, b.balance)
    assert a.shortfall.equals(b.shortfall) and a.balance_quantiles.equals(b.balance_quantiles)
    assert a.run == b.run
    c = run_engine(build_learned_inputs(obs, cats, cfg.as_of_date, "other"), "other", N)
    assert not np.array_equal(a.balance, c.balance)


def test_crn_one_off_event(learned):
    cfg, _, _, inp = learned
    D = 20
    base = run_engine(inp, "crn", N)
    ev = OneOffEvent(on=cfg.as_of_date + timedelta(days=D), amount_paise=-5_00_000_00)
    shocked = run_engine(inp, "crn", N, overrides=[ev])
    diff = shocked.balance - base.balance
    assert (diff[:, :D] == 0).all()
    assert (diff[:, D:] == -5_00_000_00).all()
    assert (shocked.shortfall["p_short"].to_numpy() >= base.shortfall["p_short"].to_numpy()).all()
    assert json.loads(shocked.run["overrides_json"])[0]["amount_paise"] == -5_00_000_00


def test_streams_are_independent(learned):
    """Dropping a component leaves other components' draws untouched."""
    cfg, obs, cats, inp = learned
    no_sales = build_learned_inputs(obs, cats, cfg.as_of_date, "t-run", with_sales=False)
    a, b = run_engine(inp, "ind", N), run_engine(no_sales, "ind", N)
    for comp in ("open_invoice_receipts", "unscheduled", "fixed_obligations"):
        assert np.array_equal(a.component_totals[comp], b.component_totals[comp]), comp


def test_outputs_and_run_record(learned):
    _, _, _, inp = learned
    res = run_engine(inp, "rec", N)
    sf = res.shortfall
    p = sf["p_short"].to_numpy()
    assert (np.diff(p) >= 0).all()
    assert (sf["expected_shortfall_uncond_paise"].to_numpy() <= sf["expected_shortfall_cond_paise"].to_numpy()).all()
    q = res.balance_quantiles
    assert (q["q05"] <= q["q25"]).all() and (q["q25"] <= q["q50"]).all() and (q["q75"] <= q["q95"]).all()
    over = np.flatnonzero(p >= 0.5)
    assert res.run["runway_days"] == (int(over[0] + 1) if over.size else None)
    for key in ("run_id", "as_of", "seed", "seed_streams", "model_versions", "input_snapshot_ids", "overrides_json"):
        assert key in res.run
    assert res.run["overrides_json"] == "[]"


def test_run_record_carries_the_engine_version(learned):
    from matsya_intelligence.engine.core import ENGINE_VERSION, run_table

    _, _, _, inp = learned
    res = run_engine(inp, "ver", 50)
    assert res.run["engine_version"] == ENGINE_VERSION
    assert res.run["model_versions"]["simulation_engine"] == ENGINE_VERSION
    assert inp.model_versions.get("simulation_engine") is None  # inputs are not mutated
    assert json.loads(run_table(res.run)["model_versions_json"][0])["simulation_engine"] == ENGINE_VERSION


def test_speed_10k_paths(learned):
    _, _, _, inp = learned
    t = time.perf_counter()
    run_engine(inp, "speed", 10_000, 90)
    elapsed = time.perf_counter() - t
    print(f"10k paths x 90 days: {elapsed:.2f}s")
    assert elapsed < 30


@pytest.mark.slow
def test_true_parameter_mode_matches_oracle():
    rows = true_vs_oracle(n_futures=200, n_paths=10_000)
    for r in rows:
        assert abs(r["engine"] - r["oracle"]) < 3 * r["se"] + 0.01, r


@pytest.mark.slow
def test_learned_mode_report():
    """Report only: learned mode against the oracle on a few pasts, and the gap-1 check."""
    lv = learned_vs_oracle(range(3), n_futures=40, n_paths=4000)
    print(lv["per_seed"], lv["mean_gap_90"])
    g = gap1(range(3), n_futures=40, n_paths=2000)
    print(g)
    assert len(lv["per_seed"]) == 3


def _fixed_z_sampler(inp, z: float):
    """Shock hook with every sector's z held at a constant value."""
    from matsya_intelligence.shocks import AR1Fit, SectorShocks

    fit = AR1Fit(rho=1.0 - 1e-12, q=1e-18, mean_T=z, var_T=0.0, loglik=0.0)
    return SectorShocks({"all": fit}).shift_sampler(["all"] * len(inp.buyer_ids))


def test_learned_shock_sign_positive_lateness_delays_receipts(learned):
    """A large positive lateness z must lower the weekly chance: receipts arrive later."""
    from dataclasses import replace

    _, _, _, inp = learned
    on_time = run_engine(replace(inp, shift_sampler=_fixed_z_sampler(inp, 0.0)), "sign", N, keep_flows=True)
    late = run_engine(replace(inp, shift_sampler=_fixed_z_sampler(inp, 3.0)), "sign", N, keep_flows=True)
    for comp in ("open_invoice_receipts", "future_invoice_receipts"):
        a = np.cumsum(on_time.flows[comp], axis=1).mean(axis=0)
        b = np.cumsum(late.flows[comp], axis=1).mean(axis=0)
        assert b[30] < 0.8 * a[30], comp
    assert (late.balance_quantiles["q50"].to_numpy()[:60] <= on_time.balance_quantiles["q50"].to_numpy()[:60]).all()


def test_learned_shock_base_preserves_posterior_chance(learned):
    """Learned inputs carry base chances whose long-run shock average is the posterior p."""
    from matsya_intelligence.shocks import shock_averaged

    _, _, _, inp = learned
    assert inp.timing.p_base is not None
    moved = False
    for p, pb in zip(inp.timing.p, inp.timing.p_base):
        lo = np.log(pb / (1 - pb))
        if np.allclose(pb, p):
            continue  # sector without a shock fit
        moved = True
        # recover the variance used from the draw furthest from 0.5: the shock average moves
        # toward 0.5 as the variance grows, so bisect on that distance
        i = int(np.argmax(np.abs(p - 0.5)))
        a, b = 1e-9, 25.0
        for _ in range(100):
            v = (a + b) / 2
            a, b = (v, b) if abs(shock_averaged(lo[i:i + 1], v)[0] - 0.5) > abs(p[i] - 0.5) else (a, v)
        assert np.allclose(shock_averaged(lo, v), np.clip(p, 1e-9, 1 - 1e-9), atol=1e-6)
    assert moved
