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
    assert (sf["es_unconditional_paise"].to_numpy() <= sf["expected_shortfall_paise"].to_numpy()).all()
    q = res.balance_quantiles
    assert (q["q05"] <= q["q25"]).all() and (q["q25"] <= q["q50"]).all() and (q["q75"] <= q["q95"]).all()
    over = np.flatnonzero(p >= 0.5)
    assert res.run["runway_days"] == (int(over[0] + 1) if over.size else None)
    for key in ("run_id", "as_of", "seed", "seed_streams", "model_versions", "input_snapshot_ids", "overrides_json"):
        assert key in res.run
    assert res.run["overrides_json"] == "[]"


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
