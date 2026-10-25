import math
from dataclasses import replace
from datetime import date, timedelta
from fractions import Fraction

import numpy as np
import pytest
from matsya_contracts.validate import validate

from matsya_intelligence.decide import Supplier, decide, decision_inputs, msmed_interest, overdraft_interest, treds_discount
from matsya_intelligence.decide.actions import candidate_actions, invoice_delta, stretch_cost
from matsya_intelligence.decide.costs import bank_rate_history, income_tax_rate
from matsya_intelligence.engine import build_learned_inputs, run_engine
from matsya_intelligence.scenarios import DateShift, RateMultiplier, apply_transforms, preset
from matsya_intelligence.synth import WorldConfig, generate_world

RUN = "decide-run"
THRESHOLD = 20_00_000_00  # Rs 20 lakh cushion: makes the default world risky (p_short(90) about 0.7)
N_SUB, N_VER = 1000, 4000
SUPPLIERS = [Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)]


# ---------------------------------------------------------------- cost formulas


def test_treds_discount_is_exact():
    # Rs 1,00,000 at 9% for 30 days: 10^7 x 0.09 x 30 / 365 = 73,972.60 paise -> 73,973
    assert treds_discount(10_000_000, 0.09, 30) == (10_000_000 - 73_973, 73_973)
    assert treds_discount(10_000_000, 0.09, 0) == (10_000_000, 0)


def test_overdraft_interest_is_exact():
    # Rs 5,00,000 at 11% for 45 days: 5 x 10^7 x 0.11 x 45 / 365 = 6,78,082.19 paise
    assert overdraft_interest(50_000_000, 0.11, 45) == 678_082
    assert overdraft_interest(50_000_000, 0.11, 0) == 0


HIST = [(date(2026, 1, 1), Fraction(55, 1000)), (date(2026, 10, 7), Fraction(575, 10000))]


def test_msmed_interest_compounds_monthly_across_a_bank_rate_change():
    P = 10_000_000
    # appointed 8 Aug; rests 8 Sep (5.50%), 8 Oct and 8 Nov (5.75%, in force from 7 Oct);
    # 8 Nov - 20 Nov is 12 of the 30 days to 8 Dec, at the rate on 20 Nov.
    f = ((1 + Fraction(3) * Fraction(55, 1000) / 12) * (1 + Fraction(3) * Fraction(575, 10000) / 12) ** 2
         * (1 + Fraction(3) * Fraction(575, 10000) / 12 * Fraction(12, 30)))
    want = math.floor(P * (f - 1) + Fraction(1, 2))
    assert msmed_interest(P, date(2026, 8, 8), date(2026, 11, 20), HIST) == want
    # the change matters: all at 5.50% gives less
    flat = [(date(2026, 1, 1), Fraction(55, 1000))]
    assert msmed_interest(P, date(2026, 8, 8), date(2026, 11, 20), flat) < want
    # one full month at 5.50%: 3 x 5.5% / 12 = 1.375%
    assert msmed_interest(P, date(2026, 8, 8), date(2026, 9, 8), HIST) == 137_500
    assert msmed_interest(P, date(2026, 8, 8), date(2026, 8, 8), HIST) == 0


def test_bank_rate_history_reads_the_constants_file():
    h = bank_rate_history()
    assert h[-1] == (date(2026, 10, 7), Fraction(575, 10000))
    assert h[0][0] == date.min  # the unsourced start date is flagged TODO in the constants file


# ---------------------------------------------------------------- one risky world


@pytest.fixture(scope="module")
def world():
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    cats = truth.transactions.select("txn_uid", "category")
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, RUN, threshold_paise=THRESHOLD)
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=10_00_000_00, suppliers=SUPPLIERS,
                           entity_type="company")
    return cfg, obs, inp, dinp


@pytest.fixture(scope="module")
def result(world):
    _, _, inp, dinp = world
    return decide(inp, dinp, RUN, n_sub=N_SUB, n_verify=N_VER)


def test_plan_meets_the_target_out_of_sample(result):
    r = result
    assert r.target_met
    do_nothing = r.baselines.filter(r.baselines["baseline"] == "do_nothing")["p_short"][0]
    assert do_nothing > 0.5  # the world is risky, so the plan has work to do
    assert r.p_short_sub <= r.eps
    # Monte Carlo error of the in-sample constraint (N_SUB paths) plus the check (N_VER paths)
    se = math.sqrt(r.eps * (1 - r.eps) / N_SUB) + math.sqrt(r.eps * (1 - r.eps) / N_VER)
    assert r.p_short_verified <= r.eps + 3 * se
    assert r.plan["p_short_after"][-1] == r.p_short_verified


def test_plan_table_and_baselines(result):
    r = result
    validate(r.contract_tables()["decisions.plans"], "decisions.plans")
    assert r.plan["rank"].to_list() == list(range(1, r.plan.height + 1))
    # signed cost kept unclamped next to the contract's non-negative cost_paise
    assert r.plan["net_cost_paise"].to_list() == [a.cost(k) for a, k in r.chosen]
    assert r.plan["cost_paise"].to_list() == [max(c, 0) for c in r.plan["net_cost_paise"]]
    assert r.plan["net_cost_paise"].sum() == r.cost_paise
    names = set(r.baselines["baseline"])
    assert names == {"do_nothing", "discount_all_eligible", "chase_largest_buyer", "plan"}
    plan_row = r.baselines.filter(r.baselines["baseline"] == "plan")
    assert plan_row["p_short"][0] == r.p_short_verified
    assert plan_row["cost_paise"][0] == r.cost_paise
    for name in ("do_nothing", "discount_all_eligible", "chase_largest_buyer"):
        row = r.baselines.filter(r.baselines["baseline"] == name)
        assert row["expected_shortfall_days"][0] >= plan_row["expected_shortfall_days"][0], name
    # amount owed is reported for every overdue invoice, chosen or not
    owed = r.msmed_owed.drop_nulls("interest_to_as_of_paise")
    assert owed.height > 0 and (owed["interest_to_as_of_paise"] > 0).all()


def test_replay_is_deterministic(world, result):
    _, _, inp, dinp = world
    again = decide(inp, dinp, RUN, n_sub=N_SUB, n_verify=N_VER)
    assert again.plan.equals(result.plan)
    assert again.record == result.record
    assert again.baselines.equals(result.baselines)


def test_overdraft_limit_is_counted_once(world, result):
    _, _, inp, dinp = world
    # draws across every draw day together stay within the limit
    drawn = sum(k * a.unit_paise for a, k in result.chosen if a.kind == "overdraft")
    assert 0 < drawn <= dinp.overdraft_limit_paise
    # the threshold is the owner cushion: headroom put into it as well would count the limit twice
    with pytest.raises(ValueError, match="cushion"):
        decide(replace(inp, threshold_paise=-dinp.overdraft_limit_paise), dinp, RUN, n_sub=100, n_verify=100)


def test_target_that_cannot_be_met_returns_the_lowest_p_short(world):
    _, _, inp, dinp = world
    bare = replace(dinp, overdraft_limit_paise=0, suppliers=[], eps=0.0)
    r = decide(inp, bare, RUN, n_sub=300, n_verify=1000)
    assert not r.target_met
    assert "cannot be met" in r.message
    nothing = r.baselines.filter(r.baselines["baseline"] == "do_nothing")["p_short"][0]
    assert r.p_short_verified < nothing


# ---------------------------------------------------------------- action effects are exact


def test_invoice_and_overdraft_deltas_match_engine_runs(world):
    _, _, inp, dinp = world
    base = run_engine(inp, "d-delta", 300, keep_flows=True)
    acts = candidate_actions(inp, dinp, base, 90, [])
    picks = [next(a for a in acts if a.kind == k) for k in ("treds_discount", "msmed_interest", "overdraft")]
    for a in picks:
        k = 3 if a.kind == "overdraft" else 1
        res = run_engine(apply_transforms(inp, a.transforms(k)), "d-delta:x", 300, seed_id="d-delta")
        got = res.balance[:, 1:] - base.balance[:, 1:]
        want = (np.where(np.arange(1, 91) >= a.detail["draw_day"], k * a.unit_paise, 0)[None, :]
                if a.kind == "overdraft" else invoice_delta(base, inp, a, 90))
        assert np.array_equal(got, np.broadcast_to(want, got.shape)), a.kind


def test_chasing_one_buyer_moves_only_its_invoices(world):
    _, _, inp, _ = world
    base = run_engine(inp, "d-chase", 300, keep_flows=True)
    b = int(inp.open_invoices.buyer[0])
    t = RateMultiplier(target="buyer", ids=[inp.buyer_ids[b]], factor=2.0, weeks=2)
    res = run_engine(apply_transforms(inp, [t]), "d-chase:x", 300, keep_flows=True, seed_id="d-chase")
    mine = inp.open_invoices.buyer == b
    assert np.array_equal(res.open_pay[:, ~mine], base.open_pay[:, ~mine])
    assert not np.array_equal(res.open_pay[:, mine], base.open_pay[:, mine])
    # within the window only: no change to payments in weeks starting after day 14 that were not moved earlier
    later = (base.open_pay[:, mine] > 21) & (res.open_pay[:, mine] > 21)
    assert np.array_equal(res.open_pay[:, mine][later], base.open_pay[:, mine][later])


def test_whole_share_partial_shift_equals_the_full_shift(world):
    _, _, inp, _ = world
    full = run_engine(apply_transforms(inp, [DateShift(target="supplier_payments", days=10)]), "d-s", 200,
                      seed_id="d-base")
    part = run_engine(apply_transforms(inp, [DateShift(target="supplier_payments", days=10, share=1.0)]), "d-s", 200,
                      seed_id="d-base")
    assert np.array_equal(full.balance, part.balance)


# ---------------------------------------------------------------- supplier stretch


def test_no_stretch_for_a_supplier_that_tightened_terms(world):
    cfg, _, inp, dinp = world
    base = run_engine(inp, "d-st", 200, keep_flows=True)
    active = preset("supplier_tightens_terms", cfg.as_of_date)
    acts = candidate_actions(apply_transforms(inp, active), dinp, base, 90, active)
    assert not [a for a in acts if a.kind == "supplier_stretch"]
    flagged = replace(dinp, suppliers=[replace(SUPPLIERS[0], tightened=True), SUPPLIERS[1]])
    acts = candidate_actions(inp, flagged, base, 90, [])
    assert {a.supplier for a in acts if a.kind == "supplier_stretch"} == {"S2"}
    # and the decision engine under that scenario never stretches anyone
    r = decide(inp, dinp, RUN, active_transforms=active, n_sub=300, n_verify=1000)
    assert "supplier_stretch" not in set(r.plan["action"])


TAX, COF = Fraction(2517, 10000), 0.10


def test_stretch_past_45_days_costs_msmed_interest_for_micro_suppliers_only():
    purch = np.full((10, 90), 1_00_000_00.0)  # Rs 1 lakh a day
    as_of = date(2026, 3, 1)  # stretches cross 31 Mar
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=1_000)
    big = Supplier("B", 0.5, "none", 30, penalty_paise=1_000)
    c_short, d_short = stretch_cost(micro, 14, purch, as_of, HIST, TAX, COF)  # 30 + 14 = 44 days: within s.15
    assert c_short == 1_000 and d_short["msmed_interest_paise"] == 0 and d_short["deferral_paise"] == 0
    c_long, d_long = stretch_cost(micro, 30, purch, as_of, HIST, TAX, COF)  # 60 days: 15 days late
    assert d_long["late_days"] == 15
    assert d_long["msmed_interest_paise"] == msmed_interest(d_long["amount_paise"], as_of + timedelta(days=1),
                                                            as_of + timedelta(days=16), HIST)
    assert d_long["deferral_paise"] > 0
    # s.23: the MSMED interest is not deductible, so it enters at full cost with no tax saving netted off
    assert c_long == 1_000 + d_long["msmed_interest_paise"] + d_long["deferral_paise"]
    assert stretch_cost(big, 30, purch, as_of, HIST, TAX, COF)[0] == 1_000


def test_43bh_is_a_deferral_worked_example():
    # as_of 1 Mar 2026, share 0.5 of Rs 1 lakh a day, 30-day terms stretched 30 days (60 in total, late).
    # Payment days t = 1..60 were accepted by 31 Mar (FY 2025-26) and now land on or after 1 Apr
    # (FY 2026-27): Rs 50,000 x 60 = Rs 30 lakh deferred one year. Later days stay in FY 2026-27.
    purch = np.full((10, 90), 1_00_000_00.0)
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=0)
    _, d = stretch_cost(micro, 30, purch, date(2026, 3, 1), HIST, TAX, COF)
    assert d["deferred_amount_years_paise"] == 30_00_000_00
    # 0.2517 x Rs 30 lakh x 10% x 1 year = Rs 75,510 (the full tax would have been Rs 7,55,100)
    assert d["deferral_paise"] == 75_510_00


def test_late_payment_in_the_same_fy_costs_no_deferral():
    purch = np.full((10, 90), 1_00_000_00.0)
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=0)
    c, d = stretch_cost(micro, 30, purch, date(2026, 5, 1), HIST, TAX, COF)  # everything inside FY 2026-27
    assert d["deferral_paise"] == 0 and d["deferred_amount_years_paise"] == 0
    assert c == d["msmed_interest_paise"] > 0  # still late: the interest is owed


def test_already_late_supplier_defers_only_when_payment_crosses_31_march():
    purch = np.full((10, 90), 1_00_000_00.0)
    late = Supplier("L", 0.5, "micro", 60, penalty_paise=0)  # 60-day terms: already past 45 days
    _, d = stretch_cost(late, 7, purch, date(2026, 3, 1), HIST, TAX, COF)
    # deduction already sits in the FY of payment; it moves only for payments on 25-31 Mar (t = 24..30)
    assert d["deferred_amount_years_paise"] == 7 * 50_000_00


def test_tax_rate_is_an_owner_input_by_entity():
    assert income_tax_rate("company") == Fraction(2517, 10000)
    assert income_tax_rate("proprietor") is None and income_tax_rate("partnership") is None
    assert income_tax_rate("proprietor", 0.3) == Fraction(3, 10)
    assert income_tax_rate(None) is None
    with pytest.raises(ValueError):
        income_tax_rate("trust")


def test_stretch_needs_a_tax_rate_unless_the_tenant_is_a_company(world):
    _, _, inp, dinp = world
    base = run_engine(inp, "d-tax", 100, keep_flows=True)
    with pytest.raises(ValueError, match="income-tax rate"):
        candidate_actions(inp, replace(dinp, entity_type="proprietor"), base, 90, [])
    acts = candidate_actions(inp, replace(dinp, entity_type="proprietor", tax_rate=0.3), base, 90, [])
    assert any(a.kind == "supplier_stretch" and a.supplier == "S1" for a in acts)
    # cost of funds defaults to the overdraft rate
    a = next(a for a in acts if a.target_id == "S1:+30d")
    b = next(a for a in candidate_actions(inp, replace(dinp, entity_type="proprietor", tax_rate=0.3,
                                                       cost_of_funds=dinp.overdraft_rate or 0.11), base, 90, [])
             if a.target_id == "S1:+30d")
    assert a.cost_paise == b.cost_paise
