"""Benchmark loaders map foreign data into valid contract tables. Skipped without the raw data
(eval/data/raw is gitignored; fetch with eval/data/download.py)."""

import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from matsya_contracts.validate import validate

EVAL = Path(__file__).resolve().parents[2] / "eval"
sys.path.insert(0, str(EVAL / "models"))
RAW = EVAL / "data" / "raw"

from matsya_intelligence.io import adapters  # noqa: E402

kaggle = pytest.mark.skipif(not (RAW / "kaggle" / "dataset.csv").exists(), reason="Kaggle data not downloaded")
berka = pytest.mark.skipif(not (RAW / "berka" / "trans.csv").exists(), reason="Berka data not downloaded")


@kaggle
def test_kaggle_tables_validate_and_respect_as_of():
    import loaders

    as_of = date(2019, 7, 1)
    t = loaders.kaggle_tables(loaders.kaggle_raw(), "U013", as_of)
    for name, df in t.items():
        validate(df, name)
    for name in ("ledger.transactions", "ledger.invoices", "labels.invoice_outcomes", "ledger.matches"):
        assert t[name]["known_at"].max().date() <= as_of, name
    out = adapters.outcomes(t, as_of)
    assert out.filter(pl.col("censored_at").is_not_null())["censored_at"].max() == as_of
    assert (out.filter(pl.col("paid_date").is_not_null())["paid_date"] <= as_of).all()


@berka
def test_berka_tables_use_only_data_before_the_loan():
    import loaders

    loan = loaders.berka_raw()["loan"].row(0, named=True)
    t, rows = loaders.berka_tables(loan["account_id"], loan["date"])
    for name, df in t.items():
        validate(df, name)
    assert rows["value_date"].max() < loan["date"]
    _, bal, src = adapters.opening_balance(t, loan["date"] - timedelta(days=1))
    assert src == "bank_reported" and bal == rows["balance_paise"][-1]
    cats = loaders.berka_categories(rows)
    assert cats.height == rows.height and cats["category"].null_count() == 0
