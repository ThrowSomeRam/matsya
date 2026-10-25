import pickle
from datetime import timedelta

import numpy as np
import polars as pl
import pytest
from matsya_contracts.validate import validate

from matsya_intelligence.engine import build_learned_inputs, run_engine
from matsya_intelligence.scenarios import (CashEvent, DateShift, Freeze, RateMultiplier, TermsChange,
                                           apply_transforms, breaking_point, from_json, preset, presets, run_scenario,
                                           to_json, transforms_hash)
from matsya_intelligence.synth import WorldConfig, generate_world

N = 1000
RUN = "scen-run"
COMPONENTS = ("open_invoice_receipts", "future_invoice_receipts", "purchases", "gst", "fixed_obligations",
              "unscheduled")


@pytest.fixture(scope="module")
def world():
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    cats = truth.transactions.select("txn_uid", "category")
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, RUN)
    base = run_engine(inp, RUN, N, keep_flows=True)
    return cfg, obs, cats, inp, base


def _changed(base, res) -> set[str]:
    names = set(base.flows) | set(res.flows)
    zero = np.zeros_like(base.balance)
    return {k for k in names if not np.array_equal(base.flows.get(k, zero), res.flows.get(k, zero))}


@pytest.mark.parametrize("name, changed", [
    ("delayed_customer_payments", {"open_invoice_receipts", "future_invoice_receipts"}),
    ("unexpected_expense", {"scenario_cash"}),
    ("reduced_sales", {"future_invoice_receipts", "purchases", "gst"}),
    ("supplier_tightens_terms", {"purchases", "gst"}),
    ("revised_payment_schedule", {"fixed_obligations"}),
])
def test_each_preset_changes_the_right_inputs_only(world, name, changed):
    cfg, _, _, inp, base = world
    ts = preset(name, cfg.as_of_date)
    s = apply_transforms(inp, ts)
    # Untouched inputs are the same objects; only the fixed list and the effects are new.
    for f in ("timing", "open_invoices", "sales", "unscheduled", "shift_sampler", "buyer_ids", "weekday_probs",
              "gst_due_days", "opening_balance_paise", "features"):
        assert getattr(s, f) is getattr(inp, f), f
    assert (s.fixed != inp.fixed) == (name == "revised_payment_schedule")
    res = run_scenario(inp, RUN, ts, name, N, base=base, keep_flows=True).result
    assert _changed(base, res) == changed


def test_preset_library_covers_the_brief():
    assert set(presets()) == {"delayed_customer_payments", "unexpected_expense", "reduced_sales",
                              "supplier_tightens_terms", "revised_payment_schedule"}


def test_overrides_never_update_learned_parameters(world):
    cfg, obs, cats, inp, _ = world
    before = pickle.dumps((inp.timing, inp.open_invoices, inp.fixed, inp.fixed_kinds,
                           {k: v.write_csv() for k, v in inp.features.items()}))
    for name in presets():
        run_scenario(inp, RUN, preset(name, cfg.as_of_date), name, 200)
    after = pickle.dumps((inp.timing, inp.open_invoices, inp.fixed, inp.fixed_kinds,
                          {k: v.write_csv() for k, v in inp.features.items()}))
    assert before == after
    # A refit after scenarios gives the same payment timing, sales and obligations, byte for byte.
    refit = build_learned_inputs(obs, cats, cfg.as_of_date, RUN)
    assert pickle.dumps(refit.timing) == pickle.dumps(inp.timing)
    for k in ("features.obligations", "features.invoice_timing", "features.sales_process"):
        assert refit.features[k].equals(inp.features[k]), k


def test_one_off_lowers_every_path_by_exactly_the_amount_from_its_date(world):
    cfg, _, _, inp, base = world
    D, A = 20, -3_00_000_00
    res = run_scenario(inp, RUN, [CashEvent(on=cfg.as_of_date + timedelta(days=D), amount_paise=A)], n_paths=N,
                       base=base).result
    diff = res.balance - base.balance
    assert (diff[:, :D] == 0).all() and (diff[:, D:] == A).all()


def test_cash_event_window_lands_inside_it_and_baseline_streams_are_kept(world):
    cfg, _, _, inp, base = world
    ev = CashEvent(on=cfg.as_of_date + timedelta(days=10), window_end=cfg.as_of_date + timedelta(days=19),
                   amount_paise=-1_00_000_00)
    res = run_scenario(inp, RUN, [ev], n_paths=N, base=base, keep_flows=True).result
    day = np.argmax(res.flows["scenario_cash"] != 0, axis=1)
    assert ((day >= 10) & (day <= 19)).all() and len(np.unique(day)) > 5
    assert _changed(base, res) == {"scenario_cash"}


def test_reduced_sales_cuts_purchases_not_salary_rent_emi(world):
    cfg, _, _, inp, base = world
    res = run_scenario(inp, RUN, preset("reduced_sales", cfg.as_of_date), n_paths=N, base=base,
                       keep_flows=True).result
    bp, sp = -base.flows["purchases"].sum(axis=1), -res.flows["purchases"].sum(axis=1)
    assert (sp <= bp).all() and sp.sum() < 0.8 * bp.sum()  # thinned orders: a subset on every path
    assert np.array_equal(res.flows["fixed_obligations"], base.flows["fixed_obligations"])
    assert {"salary", "rent", "emi"} <= set(inp.fixed_kinds)


def test_order_rate_above_one_adds_orders_on_top_of_the_base_ones(world):
    _, _, _, inp, base = world
    res = run_scenario(inp, RUN, [RateMultiplier(target="orders", factor=1.5)], n_paths=N, base=base,
                       keep_flows=True).result
    assert (-res.flows["purchases"].sum(axis=1) >= -base.flows["purchases"].sum(axis=1)).all()


def test_buyer_terms_and_sector_multiplier_touch_only_their_buyers(world):
    _, _, _, inp, _ = world
    b0, sec = inp.buyer_ids[0], inp.buyer_sectors[0]
    s = apply_transforms(inp, [TermsChange(party="buyer", ids=[b0], from_days=45, to_days=90),
                               RateMultiplier(target="sector", ids=[sec], factor=0.5)])
    assert s.scenario.due_week[0] == 90 // 7 + 1 and (s.scenario.due_week[1:] == -1).all()
    in_sec = np.array([x == sec for x in inp.buyer_sectors])
    assert (s.scenario.pay_mult[in_sec] == 0.5).all() and (s.scenario.pay_mult[~in_sec] == 1).all()
    with pytest.raises(ValueError):
        apply_transforms(inp, [RateMultiplier(target="buyer", ids=["nobody"], factor=0.5)])
    with pytest.raises(ValueError):
        DateShift(target="obligation_kind", ids=["gst"], days=3)


def test_freeze_takes_a_share_out_of_use_and_gives_it_back(world):
    cfg, _, _, inp, base = world
    res = run_scenario(inp, RUN, [Freeze(start=cfg.as_of_date + timedelta(days=5), days=10, fraction=0.5)],
                       n_paths=N, base=base).result
    diff = res.balance - base.balance
    frozen = np.round(0.5 * np.maximum(base.balance[:, 4], 0)).astype(np.int64)
    assert (diff[:, :5] == 0).all() and (diff[:, 5:15] == -frozen[:, None]).all() and (diff[:, 15:] == 0).all()


def test_outputs_and_contract_rows(world):
    cfg, _, _, inp, base = world
    sr = run_scenario(inp, RUN, preset("delayed_customer_payments", cfg.as_of_date), "delayed", N, base=base)
    e = sr.effects
    assert e.height == 90 and (e["d_p_short"] >= 0).all() and e["d_p_short"][-1] > 0
    assert sr.runway_lost_days >= 0
    tables = sr.contract_tables()
    for name, df in tables.items():
        validate(df, name)
    assert tables["forecast.runs"]["scenario_id"][0] == sr.scenario_id
    assert sr.result.run["seed"] == base.run["seed"]  # common random numbers
    assert sr.result.run["transforms_hash"] == transforms_hash(sr.transforms)


def test_replay_determinism_with_overrides(world):
    cfg, obs, cats, inp, base = world
    ts = preset("reduced_sales", cfg.as_of_date) + [
        CashEvent(on=cfg.as_of_date + timedelta(days=3), window_end=cfg.as_of_date + timedelta(days=9),
                  amount_paise=-50_000_00)]
    a = run_scenario(inp, RUN, ts, n_paths=N, base=base)
    stored = a.result.run["transforms_json"]
    inp2 = build_learned_inputs(obs, cats, cfg.as_of_date, RUN)  # fresh inputs, stored list
    b = run_scenario(inp2, RUN, from_json(stored), n_paths=N)
    assert to_json(from_json(stored)) == stored
    assert np.array_equal(a.result.balance, b.result.balance) and a.effects.equals(b.effects)
    assert a.scenario_row().equals(b.scenario_row())


def test_breaking_point_one_off_known_case(world):
    """p_short(90) under an outflow A on day D has a closed form from the baseline paths: a path
    breaches if it breaches before D or its minimum from D on is below A. Bisection must find it."""
    cfg, _, _, inp, base = world
    D, target = 10, 0.5
    bal = base.balance
    early = (bal[:, 1:D].min(axis=1) < 0)
    headroom = np.where(early, -1, bal[:, D:].min(axis=1))  # breach iff headroom - amount < 0, amount = |A|
    # smallest |A| with share(early or headroom < |A|) >= target
    need = int(np.ceil(target * N)) - int(early.sum())
    exact = float(np.sort(headroom[~early])[need - 1]) + 1 if need > 0 else 0.0
    ev = CashEvent(on=cfg.as_of_date + timedelta(days=D), amount_paise=-1)
    bp = breaking_point(inp, RUN, [ev], 0, target, benign=0.0, severe=-4.0 * max(exact, 1e6), tol=1_000_00,
                        n_paths=N)
    assert bp.monotone and bp.violations == []
    assert abs(abs(bp.magnitude) - exact) <= 1_000_00 + 1
    assert bp.p_short_at >= target


def test_breaking_point_reports_monotonicity_on_the_same_seed(world):
    _, _, _, inp, _ = world
    bp = breaking_point(inp, RUN, [RateMultiplier(target="all_buyers", factor=1.0)], 0, 0.99, n_paths=300, grid=6)
    ps = [p for _, p in bp.grid]
    assert bp.monotone == all(b >= a for a, b in zip(ps, ps[1:]))
    assert len(bp.grid) == 6 and bp.grid[0][0] == 1.0 and bp.grid[-1][0] == 0.0


def test_default_search_ranges_point_towards_the_harmful_side(world):
    from matsya_intelligence.scenarios.run import default_range

    _, _, _, inp, _ = world
    assert default_range(TermsChange(party="supplier", from_days=30, to_days=15), inp) == (30.0, 0.0)
    assert default_range(TermsChange(party="buyer", from_days=45, to_days=90), inp) == (45.0, 360.0)
    assert default_range(RateMultiplier(target="orders", factor=0.7), inp) == (1.0, 0.0)


def test_payment_multiplier_applies_to_empirical_buyers(world):
    from dataclasses import replace

    _, _, _, inp, base = world
    emp = [b for b, h in zip(inp.buyer_ids, inp.timing.hazard) if h is not None]
    assert emp  # the default world has buyers in empirical mode
    # the engine reads the hazards: dropping them changes the paths
    assert not np.array_equal(run_engine(replace(inp, timing=replace(inp.timing, hazard=None)), RUN, N).balance,
                              base.balance)
    rec = ("open_invoice_receipts", "future_invoice_receipts")
    res = run_scenario(inp, RUN, [RateMultiplier(target="buyer", ids=emp, factor=0.5)], n_paths=N, base=base,
                       keep_flows=True).result
    assert sum(res.flows[k].sum() for k in rec) < 0.95 * sum(base.flows[k].sum() for k in rec)
    # every buyer empirical, payment chance x 0: no receipts in any week starting after as_of
    n_bins = len(next(h for h in inp.timing.hazard if h is not None))
    allemp = replace(inp, timing=replace(inp.timing, hazard=[np.full(n_bins, 0.3)] * len(inp.buyer_ids),
                                         hazard_base=None))
    zero = run_scenario(allemp, RUN, [RateMultiplier(target="all_buyers", factor=0.0)], n_paths=200,
                        keep_flows=True).result
    assert zero.flows["future_invoice_receipts"].sum() == 0
    half = run_scenario(allemp, RUN, preset("delayed_customer_payments", inp.as_of), n_paths=200, keep_flows=True)
    assert sum(half.result.flows[k].sum() for k in rec) < sum(half.base.flows[k].sum() for k in rec)


def test_reduced_sales_keeps_purchases_for_orders_already_placed(world):
    _, _, _, inp, base = world
    sp = inp.features["features.sales_process"]
    L = int(sp.filter(pl.col("name") == "purchase_lag_days")["value"][0])
    for S in (0, 14):
        res = run_scenario(inp, RUN, [RateMultiplier(target="orders", factor=0.5, purchase_stickiness_days=S)],
                           n_paths=N, base=base, keep_flows=True).result
        bp, rp = base.flows["purchases"], res.flows["purchases"]
        assert np.array_equal(rp[:, : L + S + 1], bp[:, : L + S + 1])  # cut starts after lag + stickiness
        assert -rp[:, L + S + 1:].sum() < 0.7 * -bp[:, L + S + 1:].sum()
    assert preset("reduced_sales", inp.as_of)[0].stickiness_days == 14
    assert RateMultiplier(target="orders", factor=0.7).stickiness_days == 14
    with pytest.raises(ValueError):
        RateMultiplier(target="all_buyers", factor=0.7, purchase_stickiness_days=3)


def test_view_days_report(world):
    from matsya_intelligence.scenarios import preset_view_days

    cfg, _, _, inp, base = world
    assert preset_view_days("reduced_sales") == [90, 180] and preset_view_days("unexpected_expense") == [90]
    sr = run_scenario(inp, RUN, preset("reduced_sales", cfg.as_of_date), n_paths=N, base=base)
    v = sr.at_days([30, 90])
    assert v["day"].to_list() == [30, 90]
    assert v["p_short_base"][1] == base.shortfall["p_short"][89] and v["p_short"][1] == sr.result.shortfall["p_short"][89]


@pytest.mark.parametrize("sticky", [0, 14])
def test_sustained_order_cut_long_horizon_matches_order_linked_flows(world, sticky):
    """Orders x 0.7 from as_of over 365 days, same seed. Each order is dropped with chance 0.3, so
    the mean ending balance moves by -0.3 x (its receipts - its purchases - its GST) inside the
    horizon. Purchases on days 1..L+S belong to orders already placed and are not cut; GST is
    the order-driven part (baseline minus a zero-orders run), not the part from history."""
    cfg, obs, cats, _, _ = world
    H, n = 365, 2000
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, RUN, horizon=H)
    base = run_engine(inp, RUN, n, H, keep_flows=True)
    L = int(inp.features["features.sales_process"].filter(pl.col("name") == "purchase_lag_days")["value"][0])
    res = run_scenario(inp, RUN, [RateMultiplier(target="orders", factor=0.7, purchase_stickiness_days=sticky)],
                       n_paths=n, horizon=H, base=base).result
    none = run_scenario(inp, RUN, [RateMultiplier(target="orders", factor=0.0, purchase_stickiness_days=sticky)],
                        n_paths=n, horizon=H, base=base, keep_flows=True).result
    f = base.flows
    linked = (f["future_invoice_receipts"].sum(1).mean() + f["purchases"][:, L + sticky + 1:].sum(1).mean()
              + f["gst"].sum(1).mean() - none.flows["gst"].sum(1).mean())
    d = (res.balance[:, -1] - base.balance[:, -1]).astype(float)
    se = d.std(ddof=1) / np.sqrt(n)
    assert abs(d.mean() - (-0.3 * linked)) < 4 * se + 0.01 * abs(0.3 * linked)
