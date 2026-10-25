import math
from dataclasses import replace

import pytest
from matsya_contracts.validate import validate

from matsya_intelligence.attribute import attribute
from matsya_intelligence.engine import build_learned_inputs
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.config import BehaviourChange

THRESHOLD = 20_00_000_00  # Rs 20 lakh cushion: a risky world, so contributions are visible
N = 2000


def _inputs(cfg: WorldConfig, run: str = "attr"):
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    return build_learned_inputs(obs, truth.transactions.select("txn_uid", "category"), cfg.as_of_date, run,
                                threshold_paise=THRESHOLD), truth


@pytest.fixture(scope="module")
def world():
    return _inputs(WorldConfig())


@pytest.fixture(scope="module")
def result(world):
    return attribute(world[0], "attr", n_paths=N)


def test_output_and_ranking(result, world):
    inp, _ = world
    validate(result.drivers, "decisions.drivers")
    assert result.drivers.height == 10
    assert result.table.height == len(inp.open_invoice_ids) + len(inp.buyer_ids)
    d = result.table["delta_p_short_90"].to_list()
    assert d == sorted(d, reverse=True)
    assert result.drivers["delta_p_short"].to_list() == d[:10]


def test_driver_with_no_effect_contributes_nothing(world):
    inp, _ = world
    cash = inp.open_invoices.cash.copy()
    cash[0] = 0  # an invoice worth nothing cannot move the balance
    inp0 = replace(inp, open_invoices=replace(inp.open_invoices, cash=cash))
    r = attribute(inp0, "attr-zero", n_paths=N)
    row = r.table.filter(r.table["driver_id"] == inp.open_invoice_ids[0])
    for d in (30, 60, 90):
        assert row[f"delta_p_short_{d}"][0] == 0.0 and row[f"delta_es_{d}_paise"][0] == 0


def test_planted_late_buyer_ranks_first_among_buyers():
    base = WorldConfig()
    _, truth = generate_world(base, 0, 0)
    big = max(truth.buyer_records, key=lambda b: b["mix_share"])
    # The largest buyer pays at a tenth of its usual weekly chance throughout the history, so the
    # payment-timing model learns it. (A slowdown that starts mid-history is not picked up: the
    # model pools the whole history and has no change-point detection, so attribution, which ranks
    # what the model believes, misses it. See docs/track-b-decisions.md, attribution.)
    cfg = replace(base, behaviour_change=BehaviourChange(buyer_index=big["index"], at=base.start, p_multiplier=0.1))
    inp, _ = _inputs(cfg, "attr-late")
    r = attribute(inp, "attr-late", n_paths=N)
    buyers = r.table.filter(r.table["driver_kind"] == "buyer")
    assert buyers["driver_id"][0] == big["buyer_id"]
    assert buyers["delta_p_short_90"][0] > 0.05  # bringing it back to its sector's typical pace removes real risk


def test_deterministic(world, result):
    again = attribute(world[0], "attr", n_paths=N)
    assert again.table.equals(result.table) and again.drivers.equals(result.drivers)


@pytest.mark.slow
def test_top5_stable_across_seeds_within_monte_carlo_error(world):
    inp, _ = world
    n = 4000
    runs = [attribute(inp, s, n_paths=n) for s in ("seed-a", "seed-b", "seed-c")]
    p = runs[0].baseline["p_short_90"]
    se = math.sqrt(2 * p * (1 - p) / n)  # difference of two independent estimates
    tops = [set(r.table.head(5)["driver_id"]) for r in runs]
    for r in runs:
        fifth = r.table["delta_p_short_90"][4]
        vals = dict(zip(r.table["driver_id"], r.table["delta_p_short_90"]))
        for d in set.union(*tops) - set.intersection(*tops):  # drivers that move in or out of the top 5
            assert abs(vals[d] - fifth) <= 2 * se, (d, vals[d], fifth, se)
    # the leaders do not depend on the seed
    assert len({tuple(r.table.head(4)["driver_id"]) for r in runs}) == 1
