"""Turn invoice outcomes into (paid week k, open age c) per buyer.

Labels are cut at `through` = as_of - label_lag_days, because a payment can
reach us days after its value date (known_at lag). Two rules keep the
conjugate likelihood exact:
  - age c = full weeks from issue to `through`;
  - a payment in the current, partly observed week (k = c + 1) is treated as
    open at age c. Otherwise early-in-week payments would count as paid while
    open invoices get no credit for surviving part of that week, biasing p up.
The final paid date is used (part-paid invoices count as open until settled).
"""

from datetime import date, timedelta

import polars as pl


def timing_labels(outcomes: pl.DataFrame, invoices: pl.DataFrame, as_of: date, label_lag_days: int = 3,
                  never_paid_cutoff_days: int = 365) -> pl.DataFrame:
    """invoice_id, buyer_id, issue_date, state ("paid" | "open"), k (paid week), c (open age),
    d (due week = days to due // 7 + 1), past_cutoff (open longer than never_paid_cutoff_days;
    still censored in fitting)."""
    through = as_of - timedelta(days=label_lag_days)
    due = invoices.select("invoice_id", "due_date")
    df = outcomes.filter(pl.col("issue_date") <= through).join(due, on="invoice_id", how="left").with_columns(
        ((pl.col("due_date") - pl.col("issue_date")).dt.total_days() // 7 + 1).fill_null(1).cast(pl.Int32).alias("d"),
        ((pl.lit(through) - pl.col("issue_date")).dt.total_days() // 7).cast(pl.Int32).alias("c_full"),
        ((pl.col("paid_date") - pl.col("issue_date")).dt.total_days() // 7 + 1).cast(pl.Int32).alias("k_raw"),
    )
    paid = pl.col("paid_date").is_not_null() & (pl.col("paid_date") <= through) & (pl.col("k_raw") <= pl.col("c_full"))
    return df.select(
        "invoice_id", "buyer_id", "issue_date",
        pl.when(paid).then(pl.lit("paid")).otherwise(pl.lit("open")).alias("state"),
        pl.when(paid).then(pl.col("k_raw")).alias("k"),
        pl.when(paid).then(None).otherwise(pl.col("c_full")).alias("c"),
        "d",
        (~paid & ((pl.lit(through) - pl.col("issue_date")).dt.total_days() > never_paid_cutoff_days)).alias("past_cutoff"),
    )
