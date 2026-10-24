"""GST amount, owned by the sales forecaster (the obligations detector owns the date).

For month M, paid on the GSTR-3B date in month M + 1:
    due = gst_rate x (taxable sales issued in M - taxable purchases made in M) + carry
    paid = max(due, 0); carry = min(due, 0)   (excess input credit carries forward)
Taxable sales = gross - gst_paise when the invoice states its GST, else
gross / (1 + gst_rate); taxable purchases = amount / (1 + gst_rate).
"""

import polars as pl


def gst_by_month(invoices: pl.DataFrame, purchases: pl.DataFrame, gst_rate: float, carry_in: float = 0.0) -> pl.DataFrame:
    """invoices: issue_date, gross_paise[, gst_paise]; purchases: value_date, amount_paise (sign ignored).
    Returns month (first day), gst_paise (to pay next month), carry_out."""
    taxable = (pl.col("gross_paise") - pl.col("gst_paise")) if "gst_paise" in invoices.columns \
        else pl.col("gross_paise") / (1 + gst_rate)
    s = invoices.group_by(pl.col("issue_date").dt.truncate("1mo").alias("month")).agg(taxable.sum().alias("s"))
    p = purchases.group_by(pl.col("value_date").dt.truncate("1mo").alias("month")).agg(
        (pl.col("amount_paise").abs() / (1 + gst_rate)).sum().alias("p"))
    months = s.join(p, on="month", how="full", coalesce=True).fill_null(0).sort("month")
    carry, rows = carry_in, []
    for month, sv, pv in months.select("month", "s", "p").iter_rows():
        due = gst_rate * (sv - pv) + carry
        carry = min(due, 0.0)
        rows.append((month, int(round(max(due, 0.0))), carry))
    return pl.DataFrame(rows, schema={"month": pl.Date, "gst_paise": pl.Int64, "carry_out": pl.Float64}, orient="row")
