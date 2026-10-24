from datetime import timedelta

import polars as pl
import pytest
from matsya_contracts.validate import validate

from matsya_intelligence.io import adapters
from matsya_intelligence.run import run_tenant
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.replay import AS_OF_TABLES, KEEP, Replay, buyer_range, score
from matsya_intelligence.synth.world import complete_through, contract_tables_at

N = 300
DAYS = 3


@pytest.fixture(scope="module")
def truth():
    return generate_world(WorldConfig(), past_seed=0, future_seed=0)[1]


@pytest.fixture(scope="module")
def replay(truth):
    r = Replay(truth, n_paths=N)
    seen = []

    def check(step):
        # Recorded inside the loop: what the step's tables held at that moment.
        seen.append({t: r.tables[t]["known_at"].max() for t in AS_OF_TABLES} | {"as_of": step.as_of})

    r.run(DAYS, on_step=check)
    r.seen = seen
    return r


def test_no_step_sees_the_future(replay):
    for s in replay.seen:
        for t in AS_OF_TABLES:
            assert s[t] <= s["as_of"], t


def test_steps_advance_one_day_and_events_are_new(replay, truth):
    prev = truth.config.as_of
    ids = []
    for s in replay.steps:
        assert s.as_of == prev + timedelta(days=1)
        validate(s.events, "ops.events")
        if s.events.height:
            assert (s.events["at"] > prev).all() and (s.events["at"] <= s.as_of).all()
        ids += s.events["event_id"].to_list()
        prev = s.as_of
    assert len(ids) == len(set(ids))  # each credit or invoice announced once
    kinds = set(pl.concat([s.events for s in replay.steps])["kind"])
    assert kinds <= {"new_credit", "new_invoice"} and "new_credit" in kinds


def test_tables_stay_valid_and_append_only(replay, truth):
    first = contract_tables_at(truth, truth.config.as_of)
    for t, df in replay.tables.items():
        validate(df, t)
    for t in AS_OF_TABLES:  # every earlier row is still there, unchanged
        assert first[t].join(replay.tables[t], on=first[t].columns, how="anti", nulls_equal=True).height == 0, t


def test_restamps_use_the_watermark(replay, truth):
    cut = replay.as_of
    known = replay.tables["ledger.transactions"].filter(pl.col("known_at") <= cut)
    wm = complete_through(known, cut)
    assert wm < cut.date()
    lab = adapters.as_of_read(replay.tables["labels.invoice_outcomes"], ["tenant_id", "invoice_id"], cut)
    open_ = lab.filter(pl.col("paid_date").is_null())
    assert open_.height and (open_["censored_at"] == wm).all() and (open_["known_at"] == cut).all()
    assert adapters.ledger_complete_through(replay.tables, cut) == wm
    # Bank-reported balance each step = the true closing balance of that day.
    _, bal, src = adapters.opening_balance(replay.tables, cut)
    true = truth.balances.filter(pl.col("date") == cut.date())["closing_paise"][0]
    assert src == "bank_reported" and bal == true


def test_watermark_is_the_largest_reporting_lag(truth):
    cut = truth.config.as_of
    known = truth.transactions.filter(pl.col("known_at") <= cut)
    lag = (known["known_at"].dt.date() - known["value_date"]).dt.total_days().max()
    assert complete_through(known, cut) == cut.date() - timedelta(days=lag)
    # Everything value-dated on or before the watermark is already known.
    wm = complete_through(known, cut)
    late = truth.transactions.filter((pl.col("value_date") <= wm) & (pl.col("known_at") > cut))
    assert late.height == 0


def test_replaying_a_day_twice_is_identical(replay, truth):
    again = Replay(truth, n_paths=N)
    again.run(DAYS)
    for a, b in zip(replay.steps, again.steps):
        assert a.events.equals(b.events)
        for k in KEEP:
            assert a.forecast[k].equals(b.forecast[k]), k
        assert a.buyer == b.buyer
    rerun = replay.run_step()  # same day, same tables
    for k in KEEP:
        assert rerun.forecast[k].equals(replay.steps[-1].forecast[k]), k


def test_final_step_matches_a_direct_run(replay, truth):
    direct = run_tenant(contract_tables_at(truth, replay.as_of), truth.config.tenant_id, replay.as_of, "replay", N)
    for k in KEEP:
        assert direct[k].equals(replay.steps[-1].forecast[k]), k


def test_scores_and_buyer_range(replay):
    sc = score(replay)
    assert set(sc["h"]) <= {1, 7, 14, 30, 60, 90} and (sc["n"] == DAYS).all()
    assert sc["coverage_90"].is_between(0, 1).all() and (sc["mae_rs"] >= 0).all()
    br = buyer_range(replay)
    assert br.height == DAYS
    assert ((br["weeks_q05"] <= br["weeks_q50"]) & (br["weeks_q50"] <= br["weeks_q95"])).all()
    assert (br["true_weeks"] > 0).all()
