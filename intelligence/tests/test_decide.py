import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
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
from matsya_intelligence.synth.world import load_constants

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
    assert r.eps_solve == pytest.approx(0.8 * r.eps) and r.p_short_sub <= r.eps_solve
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


def test_claims_are_money_owed_not_plan_actions(result):
    r = result
    assert "msmed_interest" not in set(r.plan["action"])
    m = r.money_owed
    overdue = set(r.msmed_owed.drop_nulls("interest_to_as_of_paise")["invoice_id"])
    assert m.height > 0 and set(m["invoice_id"]) == overdue
    assert (m["net_value_paise"] == m["value_after_tax_paise"] - m["penalty_paise"]).all()
    assert m["net_value_paise"].to_list() == sorted(m["net_value_paise"].to_list(), reverse=True)


def test_run_record_has_solver_status_and_gap(result):
    rec = result.record
    assert rec["solver_status"] in (0, 1) and rec["time_limited"] == (rec["solver_status"] == 1)
    assert rec["mip_gap"] is None or rec["mip_gap"] >= 0
    assert rec["eps_solve"] == pytest.approx(0.8 * rec["eps"])


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


def _delta_gaps(inp, dinp):
    base = run_engine(inp, "d-delta", 300, keep_flows=True)
    acts = candidate_actions(inp, dinp, base, 90, [])
    picks = [next(a for a in acts if a.kind == k) for k in ("treds_discount", "msmed_interest", "overdraft")]
    for a in picks:
        k = 3 if a.kind == "overdraft" else 1
        res = run_engine(apply_transforms(inp, a.transforms(k)), "d-delta:x", 300, seed_id="d-delta")
        got = res.balance[:, 1:] - base.balance[:, 1:]
        want = np.broadcast_to(np.where(np.arange(1, 91) >= a.detail["draw_day"], k * a.unit_paise, 0)[None, :]
                               if a.kind == "overdraft" else invoice_delta(base, inp, a, 90), got.shape)
        yield a, k, got, want


def test_invoice_and_overdraft_deltas_match_engine_runs(world):
    # exact when unscheduled flows do not react to the balance (the transforms themselves)
    _, _, flat, dinp = world  # default inputs: bootstrap unscheduled flows, no balance feedback
    assert not getattr(flat.unscheduled, "balance_feedback", False)
    for a, _, got, want in _delta_gaps(flat, dinp):
        assert np.array_equal(got, want), a.kind


def test_deltas_are_close_when_flows_react_to_the_balance(world):
    # the flow forecaster pulls the balance back towards usual, so an action's cash is partly offset
    # later; the decision engine's linear model ignores that and the fresh-path run checks it
    cfg, obs, _, dinp = world
    cats = generate_world(cfg, past_seed=0, future_seed=0)[1].transactions.select("txn_uid", "category")
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, RUN, threshold_paise=THRESHOLD, flow_forecaster="quantile")
    assert inp.unscheduled.balance_feedback
    for a, k, got, want in _delta_gaps(inp, dinp):
        cash = k * a.unit_paise if a.kind == "overdraft" else a.detail.get("cash_paise", a.detail.get("principal_paise"))
        assert np.abs(got - want).max() <= 0.01 * abs(cash), a.kind


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
LAKH_A_DAY = np.full((10, 90), 1_00_000_00.0)


def _cost(s, n, as_of, gst=0.0):
    return stretch_cost(s, n, LAKH_A_DAY, as_of, HIST, TAX, COF, gst)


def test_s15_deadline_is_written_terms_capped_at_45_else_15():
    assert Supplier("A", 1, "micro", 30).msmed_deadline_days == 30
    assert Supplier("A", 1, "micro", 60).msmed_deadline_days == 45
    assert Supplier("A", 1, "micro", 30, terms_in_writing=False).msmed_deadline_days == 15


def test_stretch_past_the_agreed_date_costs_msmed_interest_for_micro_suppliers_only():
    as_of = date(2026, 5, 1)  # everything inside FY 2026-27: no deferral
    start = as_of + timedelta(days=1)
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=1_000)
    c, d = _cost(micro, 14, as_of)  # written 30 days: 14 days late, though within 45
    assert d["late_days"] == 14 and d["late_days_before"] == 0
    assert d["msmed_interest_paise"] == msmed_interest(d["amount_paise"], start, start + timedelta(days=14), HIST) > 0
    # s.23: the MSMED interest is not deductible, so it enters at full cost with no tax saving netted off
    assert d["deferral_paise"] == 0 and c == 1_000 + d["msmed_interest_paise"]
    # nothing in writing: deadline 15, already 15 days late; the stretch adds only the extra interest
    oral = Supplier("O", 0.5, "micro", 30, penalty_paise=1_000, terms_in_writing=False)
    _, d = _cost(oral, 14, as_of)
    assert (d["late_days_before"], d["late_days"]) == (15, 29)
    amt = d["amount_paise"]
    assert d["msmed_interest_paise"] == (msmed_interest(amt, start, start + timedelta(days=29), HIST)
                                         - msmed_interest(amt, start, start + timedelta(days=15), HIST))
    assert _cost(Supplier("B", 0.5, "none", 30, penalty_paise=1_000), 30, as_of)[0] == 1_000
    assert _cost(Supplier("D", 0.5, "medium", 30, penalty_paise=1_000), 30, as_of)[0] == 1_000


def test_43bh_is_a_deferral_worked_example():
    # as_of 1 Mar 2026, share 0.5 of Rs 1 lakh a day, 30-day terms stretched 30 days (late).
    # Payment days t = 1..60 were accepted by 31 Mar (FY 2025-26) and now land on or after 1 Apr
    # (FY 2026-27): Rs 50,000 x 60 = Rs 30 lakh deferred one year. Later days stay in FY 2026-27.
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=0)
    _, d = _cost(micro, 30, date(2026, 3, 1))
    assert d["deferred_amount_years_paise"] == 30_00_000_00
    # no GST: 0.2517 x Rs 30 lakh x 10% x 1 year = Rs 75,510 (the full tax would have been Rs 7,55,100)
    assert d["deferral_paise"] == 75_510_00
    # purchases carry 18% GST: only the amount net of GST is deductible
    _, g = _cost(micro, 30, date(2026, 3, 1), gst=0.18)
    want = Fraction(30_00_000_00) / Fraction(118, 100) * TAX * Fraction(1, 10)
    assert g["deferral_paise"] == math.floor(want + Fraction(1, 2)) == 63_991_53


def test_late_payment_in_the_same_fy_costs_no_deferral():
    micro = Supplier("M", 0.5, "micro", 30, penalty_paise=0)
    c, d = _cost(micro, 30, date(2026, 5, 1))
    assert d["deferral_paise"] == 0 and d["deferred_amount_years_paise"] == 0
    assert c == d["msmed_interest_paise"] > 0  # still late: the interest is owed


def test_already_late_supplier_defers_only_when_payment_crosses_31_march():
    late = Supplier("L", 0.5, "micro", 60, penalty_paise=0)  # written 60 days: capped at 45, already late
    _, d = _cost(late, 7, date(2026, 3, 1))
    # the deduction already sits in the FY of payment; it moves only for payments on 25-31 Mar (t = 24..30)
    assert d["deferred_amount_years_paise"] == 7 * 50_000_00


def test_claim_value_is_net_of_tax_on_the_recovered_interest(world):
    _, _, inp, dinp = world
    base = run_engine(inp, "d-claim", 200, keep_flows=True)
    mc = load_constants()["decision"]["msmed_claim"]
    for rate in (None, 0.30):  # company default, then an owner rate
        d = replace(dinp, tax_rate=rate)
        claims = [a for a in candidate_actions(inp, d, base, 90, []) if a.kind == "msmed_interest"]
        assert claims
        tax = Fraction(2517, 10000) if rate is None else Fraction(3, 10)
        for a in claims:
            x = a.detail
            want = (Fraction(Decimal(str(x["p_unpaid_by_claim_day"]))) * Fraction(Decimal(str(mc["recovery_share"]["value"])))
                    * x["interest_paise"] * (1 - tax))
            assert x["value_paise"] == math.floor(want + Fraction(1, 2))
            assert a.cost_paise == x["penalty_paise"] - x["value_paise"]
    with pytest.raises(ValueError, match="taxable"):
        candidate_actions(inp, replace(dinp, entity_type="proprietor", suppliers=[]), base, 90, [])


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
        candidate_actions(inp, replace(dinp, entity_type="proprietor", tenant_msme_class="medium"), base, 90, [])
    acts = candidate_actions(inp, replace(dinp, entity_type="proprietor", tax_rate=0.3), base, 90, [])
    assert any(a.kind == "supplier_stretch" and a.supplier == "S1" for a in acts)
    # cost of funds defaults to the overdraft rate
    a = next(a for a in acts if a.target_id == "S1:+30d")
    b = next(a for a in candidate_actions(inp, replace(dinp, entity_type="proprietor", tax_rate=0.3,
                                                       cost_of_funds=dinp.overdraft_rate or 0.11), base, 90, [])
             if a.target_id == "S1:+30d")
    assert a.cost_paise == b.cost_paise


# ---------------------------------------------------------------- constants overrides


def test_constants_overrides_apply_inside_the_block_only(world):
    from matsya_intelligence.decide import costs as C

    _, _, inp, dinp = world
    base = run_engine(inp, "d-ovr", 200, keep_flows=True)
    claims = lambda: {a.target_id: a for a in candidate_actions(inp, dinp, base, 90, []) if a.kind == "msmed_interest"}  # noqa: E731
    default = claims()
    assert default
    with C.overrides({"msmed_claim.recovery_share": 0.25, "msmed_claim.relationship_penalty_share": 0.2}):
        low = claims()
        assert C.constants()["msmed_claim"]["recovery_share"]["value"] == 0.25
    for i, a in low.items():
        d = default[i].detail
        assert a.detail["value_paise"] < d["value_paise"]
        assert abs(a.detail["penalty_paise"] - 2 * d["penalty_paise"]) <= 1  # each rounded to the paisa
    with C.overrides({"msmed_claim.enabled": False}):
        assert not claims()
    assert claims().keys() == default.keys()  # nothing leaks out of the block
    with pytest.raises(KeyError):
        with C.overrides({"msmed_claim.no_such_constant": 1}):
            pass


def test_decide_records_its_overrides(world):
    _, _, inp, dinp = world
    r = decide(inp, dinp, RUN, n_sub=100, n_verify=200, constants_overrides={"msmed_claim.enabled": False})
    assert r.record["constants_overrides"] == {"msmed_claim.enabled": False}
    assert r.money_owed.height == 0  # claims off: none listed either
    again = decide(inp, dinp, RUN, n_sub=100, n_verify=200, constants_overrides={"msmed_claim.enabled": False})
    assert again.plan.equals(r.plan) and again.record == r.record
