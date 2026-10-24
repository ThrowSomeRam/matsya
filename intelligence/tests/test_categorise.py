import numpy as np
import polars as pl
import pytest

from matsya_intelligence.categories import CATEGORIES
from matsya_intelligence.categorise import ABSTAIN_LABEL, Categoriser, add_override, empty_overrides
from matsya_intelligence.categorise.evaluate import TRAIN_STYLE, build, run
from matsya_intelligence.categorise.label_model import LabelModel
from matsya_intelligence.categorise.lfs import ABSTAIN, LF_NAMES, apply_lfs
from matsya_intelligence.categorise.llm import llm_label, strip_pii
from matsya_intelligence.io.types import validate
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.world import PROCESS_OF_CATEGORY


@pytest.fixture(scope="module")
def train():
    return build(TRAIN_STYLE, range(100, 102))


@pytest.fixture(scope="module")
def fitted(train):
    return Categoriser().fit(train[0])


@pytest.fixture(scope="module")
def results():
    return run()


def test_generator_and_categoriser_share_one_list():
    assert set(PROCESS_OF_CATEGORY) == set(CATEGORIES)
    truth = generate_world(WorldConfig(), past_seed=1, future_seed=0)[1]
    assert set(truth.transactions["category"]) == set(CATEGORIES)


def test_output_rows_validate(fitted, train):
    out = fitted.predict(train[0])
    validate(out, "TxnCategory")
    assert out.height == train[0].height
    assert set(out["category"]) <= set(CATEGORIES) | {ABSTAIN_LABEL}
    assert out.filter(pl.col("category") == ABSTAIN_LABEL)["needs_llm"].all()


def test_label_model_recovers_planted_accuracies():
    rng = np.random.default_rng(0)
    k, n = 6, 20000
    acc = np.array([0.95, 0.9, 0.8, 0.7, 0.6])
    y = rng.integers(k, size=n)
    votes = np.full((n, len(acc)), ABSTAIN)
    for j, a in enumerate(acc):
        fires = rng.random(n) < 0.6
        right = rng.random(n) < a
        wrong = (y + rng.integers(1, k, size=n)) % k
        votes[fires, j] = np.where(right, y, wrong)[fires]
    lm = LabelModel(k).fit(votes)
    assert np.abs(lm.accuracy - acc).max() < 0.03
    covered = (votes != ABSTAIN).any(axis=1)
    assert (lm.predict_proba(votes).argmax(axis=1) == y)[covered].mean() > 0.9


def test_benchmark_beats_or_matches_baselines(results):
    for test in ("in_template", "held_out"):
        r = results[test]
        assert r["pipeline"]["macro_f1"] >= r["rules_only"]["macro_f1"] - 0.01
        assert r["pipeline"]["macro_f1"] > 0.95
        assert r["pipeline"]["abstain_share"] < 0.02
        assert r["pipeline"]["n"] > 1000
    # Template hold-out is visible: a classifier alone degrades on unseen formats.
    assert results["held_out"]["classifier_only"]["macro_f1"] < results["in_template"]["classifier_only"]["macro_f1"] - 0.1


def test_owner_correction_wins_and_enters_training(fitted, train):
    txns = train[0]
    pred = fitted.predict(txns)
    row = pred.filter(pl.col("category") == "supplier_payment").row(0, named=True)
    overrides = add_override(empty_overrides(), row["tenant_id"], row["txn_uid"], "rent")

    again = fitted.predict(txns, overrides=overrides).filter(pl.col("txn_uid") == row["txn_uid"])
    assert again["category"][0] == "rent"
    assert again["source"][0] == "owner"
    assert again["confidence"][0] == 1.0

    ts = fitted.training_set(txns, overrides)
    mine = ts.filter(pl.col("txn_uid") == row["txn_uid"])
    assert mine.height == 1
    assert mine["category"][0] == "rent" and mine["origin"][0] == "owner"
    assert ts.height == fitted.training_set(txns).height  # replaced, not duplicated

    refit = Categoriser().fit(txns, overrides)
    assert refit.predict(txns, overrides=overrides).filter(pl.col("txn_uid") == row["txn_uid"])["category"][0] == "rent"


def test_latest_override_wins():
    o = add_override(empty_overrides(), "t", "x", "rent")
    o = add_override(o, "t", "x", "salary")
    assert o.height == 1 and o["category"][0] == "salary"
    with pytest.raises(ValueError):
        add_override(o, "t", "x", "not_a_class")


def test_abstain_path_and_llm_interface(train):
    cat = Categoriser(rule_threshold=1.01, model_threshold=1.01).fit(train[0])
    txns = train[0].head(20)
    out = cat.predict(txns)
    assert (out["category"] == ABSTAIN_LABEL).all() and out["needs_llm"].all()
    assert llm_label(["x"], CATEGORIES) == [None]  # stub abstains
    seen = []

    def fake_llm(narrations, categories):
        seen.extend(narrations)
        return ["misc_expense"] * len(narrations)

    filled = cat.predict(txns, llm=fake_llm)
    assert (filled["source"] == "llm").all() and not filled["needs_llm"].any()
    assert all("@" not in n for n in seen)  # VPAs stripped before the LLM sees them


def test_strip_pii():
    assert strip_pii("UPI-RAMESH-ramesh@okicici-HDFC0001234-123456789012-RENT") == "UPI-RAMESH-<VPA>-HDFC#-#-RENT"


def test_every_lf_fires_somewhere(train):
    votes = apply_lfs(train[0])
    fired = (votes != ABSTAIN).any(axis=0)
    assert fired.all(), [n for n, f in zip(LF_NAMES, fired) if not f]
