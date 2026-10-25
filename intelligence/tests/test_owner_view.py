import json
from pathlib import Path

import numpy as np
import pytest

from matsya_intelligence.decide import Supplier, decision_inputs
from matsya_intelligence.engine import build_learned_inputs
from matsya_intelligence.present.owner_view import (OwnerView, build_owner_view, fmt_pct, fmt_rs, group_en_in,
                                                    strings_and_numbers)
from matsya_intelligence.present.tenant_run import compute_tenant_run, p_short_by_day
from matsya_intelligence.synth import WorldConfig, generate_world

DEMO_JSON = Path(__file__).resolve().parents[2] / "eval" / "demo" / "owner_view.json"
N = 1000


def test_indian_money_format():
    assert group_en_in(12345678) == "1,23,45,678"
    assert group_en_in(999) == "999" and group_en_in(1000) == "1,000" and group_en_in(100000) == "1,00,000"
    assert fmt_rs(4_20_000_00) == "Rs 4.2 lakh"
    assert fmt_rs(1_30_00_000_00) == "Rs 1.3 crore"
    assert fmt_rs(45_300_00) == "Rs 45,300"
    assert fmt_rs(10_00_000_00) == "Rs 10 lakh"
    assert fmt_rs(-2_10_000_00) == "minus Rs 2.1 lakh"
    assert fmt_pct(0.0) == "0%" and fmt_pct(0.004) == "under 1%" and fmt_pct(0.346) == "35%"


@pytest.fixture(scope="module")
def run_and_view():
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, 0, 0)
    cats = truth.transactions.select("txn_uid", "category")
    run = "owner-view-test"
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, run, threshold_paise=20_00_000_00)
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=10_00_000_00,
                           suppliers=[Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)],
                           entity_type="company")
    tr = compute_tenant_run(obs, inp, dinp, run, n_paths=N, plan_method="cvar",
                            preset_names=["delayed_customer_payments", "unexpected_expense"])
    return tr, build_owner_view(tr)


def test_payload_validates_and_round_trips(run_and_view):
    _, v = run_and_view
    again = OwnerView.model_validate_json(v.model_dump_json())
    assert again == v
    assert len(v.fan.days) == 90 and len(v.risk_by_day) == 90
    assert [r.day for r in v.headline.risk] == [30, 60, 90]
    assert all(d.calibrated == (d.day <= 30) for d in v.fan.days)
    assert all(d.lo90_paise <= d.lo50_paise <= d.median_paise <= d.hi50_paise <= d.hi90_paise for d in v.fan.days)


def test_every_rupee_figure_is_the_engines(run_and_view):
    tr, v = run_and_view
    base = tr.baseline
    assert v.headline.balance_today.paise == tr.inputs.opening_balance_paise
    assert v.fan.threshold.paise == tr.inputs.threshold_paise
    q = base.balance_quantiles
    for col, f in (("q05", "lo90_paise"), ("q25", "lo50_paise"), ("q50", "median_paise"), ("q75", "hi50_paise"),
                   ("q95", "hi90_paise")):
        # no calibration history: every day, calibrated or not, is the engine's own band
        assert [getattr(d, f) for d in v.fan.days] == q[col].to_list()
    p = p_short_by_day(base)
    assert [r.chance.value for r in v.headline.risk] == [float(p[d - 1]) for d in (30, 60, 90)]
    # the baseline is the decision engine's fresh do-nothing run: the plan's risks share its paths
    assert np.allclose([r.before for r in v.risk_by_day], tr.plan.p_short_by_day["do_nothing"])
    assert [r.after for r in v.risk_by_day] == [float(x) for x in tr.plan.p_short_by_day["plan"]]
    assert [a.cost.paise for a in v.plan.actions] == tr.plan.plan["cost_paise"].to_list()
    assert [a.risk_after.value for a in v.plan.actions] == tr.plan.plan["p_short_after"].to_list()
    assert v.plan.total_cost.paise == sum(tr.plan.plan["cost_paise"])
    assert v.plan.risk_after.value == tr.plan.p_short_verified
    oi = tr.inputs.open_invoices
    cash = dict(zip(tr.inputs.open_invoice_ids, oi.cash.tolist()))
    att = {(r["driver_kind"], r["driver_id"]): r for r in tr.attribution.table.iter_rows(named=True)}
    for d in v.drivers:
        r = att[(d.kind, d.driver_id)]
        assert d.contribution.value == max(r["delta_p_short_90"], 0.0)
        assert d.contribution_es.paise == max(r["delta_es_90_paise"], 0)
        if d.kind == "invoice":
            assert d.amount.paise == cash[d.driver_id]
    owed = {r["invoice_id"]: r for r in tr.plan.msmed_owed.iter_rows(named=True)}
    for o in v.owed.items:
        assert o.amount.paise == owed[o.invoice_id]["principal_paise"]
        assert o.interest.paise == owed[o.invoice_id]["interest_to_as_of_paise"]
        assert o.days_late == owed[o.invoice_id]["days_overdue"]
    for w, (_, _, sr, _) in zip(v.what_ifs, tr.scenarios):
        assert w.risk_by_day == [float(x) for x in sr.result.shortfall["p_short"]]
        assert w.change_90 == pytest.approx(float(sr.effects["d_p_short"][89]))
        assert w.runway_lost_days == sr.runway_lost_days


def test_strings_hold_no_numbers_outside_the_payload(run_and_view):
    _, v = run_and_view
    texts, allowed = strings_and_numbers(v)
    assert texts
    from matsya_intelligence.present.owner_view import NUMBER

    for t in texts:
        for tok in NUMBER.findall(t):
            assert tok in allowed, (tok, t)


def test_no_em_dash_or_jargon_in_strings(run_and_view):
    _, v = run_and_view
    texts, _ = strings_and_numbers(v)
    for t in texts:
        assert "—" not in t
        for word in ("p_short", "CVaR", "Monte Carlo", "quantile"):
            assert word not in t


def test_precomputed_demo_payload_validates():
    if not DEMO_JSON.exists():
        pytest.skip("eval/demo/owner_view.json not built (eval/models/owner_view_demo.py)")
    v = OwnerView.model_validate(json.loads(DEMO_JSON.read_text()))
    texts, allowed = strings_and_numbers(v)
    from matsya_intelligence.present.owner_view import NUMBER

    assert all(tok in allowed for t in texts for tok in NUMBER.findall(t))
    assert len(v.what_ifs) == 5
