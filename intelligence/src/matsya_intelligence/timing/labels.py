"""Turn invoice outcomes into (paid week k, open age c) per buyer.

Input is the adapter's timing view of labels.invoice_outcomes as known at as_of
(io.adapters.outcomes): only rows with known_at <= as_of, so no extra label lag.
A label is censored when censored_at is set (open at as_of, or a TReDS discount),
with paid_date then null.

Two rules keep the conjugate likelihood exact:
  - age c = full weeks from issue to the censoring date, min(censored_at, as_of);
  - a payment in the current, partly observed week (k = c + 1 with c measured to
    as_of) is treated as open at age c. Otherwise early-in-week payments would
    count as paid while open invoices get no credit for surviving part of that
    week, biasing p up.
The final paid date is used (part-paid invoices count as open until settled).
"""

from datetime import date

import polars as pl


def timing_labels(outcomes: pl.DataFrame, invoices: pl.DataFrame, as_of: date,
                  never_paid_cutoff_days: int = 365) -> pl.DataFrame:
    """invoice_id, buyer_id, issue_date, state ("paid" | "open"), k (paid week), c (open age),
    d (due week = days to due // 7 + 1), past_cutoff (open longer than never_paid_cutoff_days;
    still censored in fitting)."""
    due = invoices.select("invoice_id", "due_date")
    cut = pl.min_horizontal(pl.col("censored_at").fill_null(pl.lit(as_of)), pl.lit(as_of))
    df = outcomes.filter(pl.col("issue_date") <= as_of).join(due, on="invoice_id", how="left").with_columns(
        ((pl.col("due_date") - pl.col("issue_date")).dt.total_days() // 7 + 1).fill_null(1).cast(pl.Int32).alias("d"),
        ((pl.lit(as_of) - pl.col("issue_date")).dt.total_days() // 7).cast(pl.Int32).alias("c_full"),
        ((cut - pl.col("issue_date")).dt.total_days() // 7).cast(pl.Int32).alias("c_cut"),
        ((pl.col("paid_date") - pl.col("issue_date")).dt.total_days() // 7 + 1).cast(pl.Int32).alias("k_raw"),
    )
    paid = (pl.col("censored_at").is_null() & pl.col("paid_date").is_not_null() & (pl.col("paid_date") <= as_of)
            & (pl.col("k_raw") <= pl.col("c_full")))
    return df.select(
        "invoice_id", "buyer_id", "issue_date",
        pl.when(paid).then(pl.lit("paid")).otherwise(pl.lit("open")).alias("state"),
        pl.when(paid).then(pl.col("k_raw")).alias("k"),
        pl.when(paid).then(None).otherwise(pl.col("c_cut")).alias("c"),
        "d",
        (~paid & ((cut - pl.col("issue_date")).dt.total_days() > never_paid_cutoff_days)).alias("past_cutoff"),
    )
