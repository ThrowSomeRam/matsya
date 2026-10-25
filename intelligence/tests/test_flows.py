from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from matsya_intelligence.engine import build_learned_inputs, run_engine
from matsya_intelligence.flows import QUANTILES, calendar_features, fit_flow_forecaster
from matsya_intelligence.flows.forecaster import UNIT, _inverse, _pit
from matsya_intelligence.synth import WorldConfig, generate_world

AS_OF = date(2026, 3, 31)


def _history(days: int, amount_fn, cat: str = "misc_expense", start_balance: float = 0.0):
    """Synthetic transactions: one row per day with amount_fn(i, day, prev_closing) paise; the
    returned opening (balance at as_of) is consistent with start_balance."""
    start = AS_OF - timedelta(days=days - 1)
    rows = []
    bal = start_balance
    amounts = []
    for i in range(days):
        d = start + timedelta(days=i)
        a = int(amount_fn(i, d, bal))
        amounts.append(a)
        bal += a
        rows.append({"txn_uid": f"t{i}", "value_date": d, "amount_paise": a})
    txns = pl.DataFrame(rows, schema={"txn_uid": pl.String, "value_date": pl.Date, "amount_paise": pl.Int64})
    cats = pl.DataFrame({"txn_uid": txns["txn_uid"], "category": [cat] * days})
    return txns, cats, int(round(bal))


def test_calendar_features_flags():
    days = [date(2026, 1, 31), date(2026, 2, 2), date(2026, 11, 1), date(2026, 11, 8), date(2026, 11, 9)]
    X = calendar_features(days, [date(2026, 11, 8)])
    assert X.shape == (5, 13)
    assert X[0, 11] == 1 and X[1, 11] == 0  # month-end: last 3 days
    assert X[1, 0] == 1  # 2 Feb 2026 is a Monday
    assert list(X[:, 12]) == [0, 0, 1, 1, 0]  # festival day and the 7 days before
    assert X[2, 6:11].sum() == 0 and X[3, 6] == 1  # day-of-month bands: 1-5 is the base, 6-10 the first dummy


def test_quantile_function_and_pit_round_trip():
    qv = np.array([[-5.0, -1.0, 0.0, 2.0, 10.0]] * 4)
    u = np.array([0.05, 0.4, 0.75, 0.99])
    x = _inverse(qv, u)
    assert x[0] == -5.0 and x[2] == 2.0
    back = _pit(qv, x, np.random.default_rng(0))
    assert back == pytest.approx(u)
    # ties: a value on a flat stretch draws within it
    flat = np.array([[0.0, 0.0, 0.0, 0.0, 3.0]] * 2000)
    p = _pit(flat, np.zeros(2000), np.random.default_rng(1))
    assert 0.0 <= p.min() and p.max() <= 0.75 and abs(p.mean() - 0.375) < 0.02


def test_weekday_effect_is_recovered():
    rng = np.random.default_rng(0)
    txns, cats, opening = _history(400, lambda i, d, b: (-20 * UNIT if d.weekday() == 0 else 0) - rng.integers(0, UNIT))
    ff = fit_flow_forecaster(txns, cats, AS_OF, opening, use_chronos=False)
    assert ff.mode == "quantile"
    days = [date(2026, 4, 6), date(2026, 4, 7)]  # Monday, Tuesday
    q = ff.quantiles(days, np.full(2, ff.usual_balance))["misc_expense"] / UNIT
    assert q[0, 2] == pytest.approx(-20.5, abs=0.5) and q[1, 2] == pytest.approx(-0.5, abs=0.5)


def test_mean_reversion_pulls_the_balance_back():
    # the owner withdraws more when the balance is high and puts money in when it is low
    rng = np.random.default_rng(1)
    usual = 20 * 100 * UNIT  # Rs 20 lakh
    shocks = {100: 30, 250: -25, 400: 20}  # Rs lakh one-offs that knock the balance away from usual

    def flow(i, d, b):
        return int(-0.05 * (b - usual) + rng.normal(0, 2 * UNIT) + shocks.get(i, 0) * 100 * UNIT)

    txns, cats, opening = _history(500, flow, start_balance=usual)
    ff = fit_flow_forecaster(txns, cats, AS_OF, opening, use_chronos=False)
    assert ff.coef["misc_expense"][2, -1] < 0  # median flow falls as the balance rises above usual
    hi = np.full((2000, 31), ff.usual_balance + 5 * ff.balance_spread)
    lo = np.full((2000, 31), ff.usual_balance - 5 * ff.balance_spread)
    s_hi = ff.sample(2000, 30, np.random.default_rng(2), pre_balance=hi)[:, 1:].sum(axis=1).mean()
    s_lo = ff.sample(2000, 30, np.random.default_rng(2), pre_balance=lo)[:, 1:].sum(axis=1).mean()
    assert s_hi < 0 < s_lo


def test_persistence_is_estimated_and_reproduced():
    rng = np.random.default_rng(3)
    z = [0.0]
    for _ in range(599):
        z.append(0.6 * z[-1] + np.sqrt(1 - 0.36) * rng.standard_normal())
    z = np.array(z)
    txns, cats, opening = _history(600, lambda i, d, b: int(z[i] * 3 * UNIT))
    ff = fit_flow_forecaster(txns, cats, AS_OF, opening, use_chronos=False)
    assert ff.rho["misc_expense"] == pytest.approx(0.6, abs=0.1)
    s = ff.sample(4000, 60, np.random.default_rng(4))[:, 1:] / UNIT
    lag1 = np.mean([np.corrcoef(s[:, t], s[:, t + 1])[0, 1] for t in range(0, 59, 5)])
    assert lag1 == pytest.approx(ff.rho["misc_expense"], abs=0.1)


def test_short_history_falls_back():
    txns, cats, opening = _history(30, lambda i, d, b: -UNIT)
    boot = object()
    ff = fit_flow_forecaster(txns, cats, AS_OF, opening, bootstrap=boot, use_chronos=False)
    assert ff.mode == "bootstrap" and ff.bootstrap is boot and not ff.balance_feedback


def test_learned_inputs_use_the_forecaster_and_runs_replay():
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    cats = truth.transactions.select("txn_uid", "category")
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, "flows-run", flow_forecaster="quantile")
    assert inp.model_versions["unscheduled"] == "quantile-reg-v1" and inp.unscheduled.balance_feedback
    a, b = run_engine(inp, "flows-run", 500, keep_flows=True), run_engine(inp, "flows-run", 500, keep_flows=True)
    assert np.array_equal(a.balance, b.balance)
    assert set(QUANTILES) == {0.05, 0.25, 0.5, 0.75, 0.95}
    default = build_learned_inputs(obs, cats, cfg.as_of_date, "flows-run")
    assert default.model_versions["unscheduled"] == "bootstrap-8w-v0"


def test_short_history_uses_chronos_when_installed():
    pytest.importorskip("chronos")
    txns, cats, opening = _history(40, lambda i, d, b: -20 * UNIT if i % 7 == 0 else -UNIT // 2)
    ff = fit_flow_forecaster(txns, cats, AS_OF, opening, horizon=14)
    assert ff.mode == "chronos" and not ff.balance_feedback
    q = ff.fixed_quantiles["misc_expense"]
    assert q.shape == (14, 5) and (np.diff(q, axis=1) >= 0).all()
    spike = (AS_OF + timedelta(days=1) - (AS_OF - timedelta(days=39))).days  # day index in the series of day 1
    weekly = [d for d in range(14) if (spike + d) % 7 == 0]
    assert all(q[d, 2] < -10 for d in weekly) and all(q[d, 2] > -5 for d in range(14) if d not in weekly)
    s = ff.sample(200, 14, np.random.default_rng(0))
    assert s.shape == (200, 15) and (s[:, 0] == 0).all()
