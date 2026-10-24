"""Obligations detector: scheduled outflows for the next horizon_days.

Owns, per docs/track-b-decisions.md:
  - statutory dates (TDS, PF, ESI, GST) from the calendar, with amounts for TDS,
    PF and ESI from the median of past payments of that kind. GST's date only:
    its amount is the sales forecaster's (amount_paise null, amount_owner
    "sales_forecaster"). A statutory kind the business has never paid is not
    emitted, except GST.
  - recurring fixed outflows (salary, rent, EMI, professional fees) found by
    recurrence detection on categorised outflows.
Supplier payments are not here: the sales forecaster owns them as linked purchases.
"""

import re
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.io.types import OBLIGATION
from matsya_intelligence.obligations.calendar import statutory_dates
from matsya_intelligence.obligations.recurrence import Recurrence, detect_recurrences

RECURRING_CATEGORIES = ("salary", "rent", "emi", "professional_fee")
STATUTORY_KINDS = {
    "tds": ("tds_payment", None),
    "pf": ("pf_esi", re.compile(r"EPF|PROVIDENT")),
    "esi": ("pf_esi", re.compile(r"ESI")),
}
EXCLUDED_CATEGORIES = ("supplier_payment", "customer_receipt")  # owned by the sales forecaster


def _statutory_amount(outflows: pl.DataFrame, kind: str) -> tuple[int, float] | None:
    category, rx = STATUTORY_KINDS[kind]
    g = outflows.filter(pl.col("category") == category)
    if rx is not None:
        g = g.filter(pl.col("narration_raw").str.to_uppercase().map_elements(
            lambda n: bool(rx.search(n)), return_dtype=pl.Boolean))
    if g.height == 0:
        return None
    amounts = np.abs(g.sort("value_date")["amount_paise"].to_numpy()[-6:])
    med = float(np.median(amounts))
    spread = float(np.median(np.abs(amounts - med))) / med if med else 1.0
    return int(med), float(np.clip(1 - spread, 0.0, 1.0))


def detect_obligations(txns: pl.DataFrame, categories: pl.DataFrame, as_of: date, horizon_days: int = 90,
                       qrmp: bool = False, state_group: int = 1) -> pl.DataFrame:
    """txns: Txn rows; categories: txn_uid, category (features.txn_category). Returns Obligation rows."""
    tenant = txns["tenant_id"][0]
    until = as_of + timedelta(days=horizon_days)
    outflows = (
        txns.filter((pl.col("amount_paise") < 0) & (pl.col("value_date") <= as_of))
        .join(categories.select("txn_uid", "category"), on="txn_uid")
        .filter(~pl.col("category").is_in(EXCLUDED_CATEGORIES))
    )
    rows = []
    for kind, due in statutory_dates(as_of + timedelta(days=1), until, qrmp, state_group):
        if kind == "gst":
            rows.append((f"gst-{due:%Y%m%d}", kind, due, None, 1.0, "sales_forecaster"))
            continue
        est = _statutory_amount(outflows, kind)
        if est is not None:
            rows.append((f"{kind}-{due:%Y%m%d}", kind, due, est[0], est[1], "obligations_detector"))

    recs: list[Recurrence] = detect_recurrences(outflows.filter(pl.col("category").is_in(RECURRING_CATEGORIES)))
    for i, r in enumerate(recs):
        for due in r.next_dates(as_of, until):
            rows.append((f"{r.category}-{i}-{due:%Y%m%d}", r.category, due, r.amount_paise, r.confidence,
                         "obligations_detector"))
    out = pl.DataFrame(rows, schema=[c for c in OBLIGATION if c != "tenant_id"], orient="row")
    return out.select(pl.lit(tenant).alias("tenant_id"), pl.all()).cast(OBLIGATION).sort("due_date", "kind")
