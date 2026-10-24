from datetime import date, timedelta

import polars as pl
import pytest

from matsya_intelligence.categorise import Categoriser
from matsya_contracts.validate import validate
from matsya_intelligence.obligations import detect_obligations, detect_recurrences, statutory_dates
from matsya_intelligence.obligations.detector import EXCLUDED_CATEGORIES, RECURRING_CATEGORIES, STATUTORY_KINDS
from matsya_intelligence.io.adapters import track_b_inputs
from matsya_intelligence.synth import WorldConfig, generate_world

HORIZON = 90


def test_statutory_calendar():
    dates = statutory_dates(date(2026, 4, 1), date(2026, 10, 31))
    by_kind = {k: [d for kk, d in dates if kk == k] for k in ("tds", "pf", "esi", "gst")}
    assert date(2026, 4, 30) in by_kind["tds"] and date(2026, 4, 7) not in by_kind["tds"]  # March TDS
    assert date(2026, 5, 7) in by_kind["tds"]
    assert all(d.day == 15 for d in by_kind["pf"] + by_kind["esi"])
    assert date(2026, 9, 20).weekday() == 6
    assert date(2026, 9, 21) in by_kind["gst"] and date(2026, 9, 20) not in by_kind["gst"]
    q = [d for k, d in statutory_dates(date(2026, 1, 1), date(2026, 12, 31), qrmp=True, state_group=2) if k == "gst"]
    assert [(d.month, d.day) for d in q] == [(1, 24), (4, 24), (7, 24), (10, 24)]


def _stream(start: date, step_days: int, n: int, amount: int, narration: str, category: str, jitter=None):
    rows = []
    for i in range(n):
        d = start + timedelta(days=step_days * i + (jitter[i] if jitter else 0))
        rows.append({"value_date": d, "amount_paise": -amount, "narration_raw": narration, "category": category})
    return pl.DataFrame(rows)


def test_recurrence_periods_and_rejection():
    weekly = _stream(date(2026, 1, 2), 7, 8, 5_000_00, "NEFT DR-HDFC0001-RAVI KUMAR-WAGES-X", "salary")
    quarterly = _stream(date(2025, 1, 10), 91, 5, 90_000_00, "ACH D- SIDBI LOAN-ABC", "emi")
    jittered = _stream(date(2025, 6, 5), 30, 8, 2_00_000_00, "UPI-ANITA SHAH-anita@ybl-RENT", "rent",
                       jitter=[0, 1, -1, 2, 0, -2, 1, 0])
    irregular = _stream(date(2025, 6, 5), 1, 4, 1_000_00, "UPI-RANDOM SHOP-x@ybl-PAY", "professional_fee",
                        jitter=[0, 9, 40, 41])
    found = {r.category: r for r in detect_recurrences(pl.concat([weekly, quarterly, jittered, irregular]))}
    assert found["salary"].period_days == 7
    assert found["emi"].period_days == 90
    assert found["rent"].period_days == 30 and found["rent"].amount_paise == 2_00_000_00
    assert "professional_fee" not in found
    assert found["salary"].confidence > found["rent"].confidence  # zero spread beats jittered


def _score(seed: int, use_categoriser: Categoriser | None = None):
    cfg = WorldConfig()
    tables, truth = generate_world(cfg, past_seed=seed, future_seed=0)
    obs = track_b_inputs(tables, cfg.as_of)
    cats = (use_categoriser.predict(obs["transactions"]) if use_categoriser
            else truth.transactions.select("txn_uid", "category"))
    det = detect_obligations(obs["transactions"], cats, cfg.as_of_date, HORIZON)
    validate(det, "features.obligations")
    a = cfg.as_of_date
    tr = truth.obligations.filter(pl.col("due_date").is_between(a + timedelta(days=1), a + timedelta(days=HORIZON)))
    key = lambda df: set(zip(df["kind"], df["due_date"]))
    hit = key(tr) & key(det)
    amounts = tr.filter(pl.col("kind") != "gst").join(det, on=["kind", "due_date"], suffix="_d")
    rel_err = ((amounts["amount_paise_d"] - amounts["amount_paise"]).abs() / amounts["amount_paise"]).to_list()
    return len(hit) / len(key(tr)), len(hit) / len(key(det)), rel_err, tr, det


def test_detector_vs_truth():
    for seed in range(5):
        recall, precision, rel_err, tr, det = _score(seed)
        assert recall == 1.0 and precision == 1.0, seed
        assert max(rel_err) < 0.01, seed
        # Statutory dates exact, and GST carries no amount (the sales forecaster owns it).
        stat = lambda df: set(zip(df["kind"], df["due_date"])) & {(k, d) for k, d in zip(df["kind"], df["due_date"])
                                                                  if k in ("tds", "pf", "esi", "gst")}
        assert stat(tr) == stat(det)
        gst = det.filter(pl.col("kind") == "gst")
        assert gst["amount_paise"].null_count() == gst.height and (gst["amount_owner"] == "sales_forecaster").all()


def test_detector_with_categoriser_labels():
    from matsya_intelligence.categorise.evaluate import build

    train, _ = build({}, range(300, 303))
    cat = Categoriser().fit(train)
    recall, precision, rel_err, _, _ = _score(7, use_categoriser=cat)
    assert recall >= 0.9 and precision >= 0.9
    assert sorted(rel_err)[len(rel_err) // 2] < 0.01


def test_no_double_counting_supplier_payments():
    assert "supplier_payment" in EXCLUDED_CATEGORIES
    used = set(RECURRING_CATEGORIES) | {c for c, _ in STATUTORY_KINDS.values()}
    assert not used & set(EXCLUDED_CATEGORIES)
    # A perfectly regular monthly supplier payment is still not an obligation.
    tables, truth = generate_world(WorldConfig(), past_seed=1, future_seed=0)
    obs = track_b_inputs(tables, WorldConfig().as_of)
    txns = obs["transactions"]
    a = WorldConfig().as_of_date
    fake = pl.DataFrame([{
        **txns.row(0, named=True), "txn_uid": f"fake{i}", "value_date": a - timedelta(days=30 * i),
        "amount_paise": -3_33_333_00, "direction": "debit", "narration_raw": "NEFT DR-HDFC0001-STEEL CO PVT LTD-PO-X",
    } for i in range(1, 7)], schema=txns.schema)
    cats = pl.concat([truth.transactions.select("txn_uid", "category"),
                      pl.DataFrame({"txn_uid": fake["txn_uid"], "category": ["supplier_payment"] * 6})])
    det = detect_obligations(pl.concat([txns, fake]), cats, a, HORIZON)
    assert not det.filter(pl.col("amount_paise") == 3_33_333_00).height  # distinct from the Rs 3 lakh EMI
