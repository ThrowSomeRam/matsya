"""Table shapes Track B reads and writes.

Contract tables come from matsya_contracts (the only definitions; nothing is
duplicated here). The frames below are Track B's internal inputs: what the
adapters (io.adapters) build from as-of reads of the contract tables, so the
models never see a contract column directly.
"""

import polars as pl
from matsya_contracts.validate import schema

# Contract tables Track B reads (Track A writes).
TXN = schema("ledger.transactions")
BALANCE = schema("ledger.balances")
INVOICE = schema("ledger.invoices")
MATCH = schema("ledger.matches")
INVOICE_OUTCOME = schema("labels.invoice_outcomes")
BUYER = schema("ref.buyers")
TENANT_BUYER = schema("ref.tenant_buyers")
TXN_CORRECTION = schema("labels.txn_corrections")

# Contract tables Track B writes.
TXN_CATEGORY = schema("features.txn_category")
OBLIGATION = schema("features.obligations")
INVOICE_TIMING = schema("features.invoice_timing")
SALES_PROCESS = schema("features.sales_process")
SHOCK_STATE = schema("forecast.shock_state")
RUN = schema("forecast.runs")
BALANCE_QUANTILE = schema("forecast.balance_quantiles")
SHORTFALL = schema("forecast.shortfall")

READS = ("ledger.transactions", "ledger.balances", "ledger.invoices", "ledger.matches", "labels.invoice_outcomes",
         "labels.txn_corrections", "ref.buyers", "ref.tenant_buyers")
WRITES = ("features.txn_category", "features.obligations", "features.invoice_timing", "features.sales_process",
          "forecast.shock_state", "forecast.runs", "forecast.balance_quantiles", "forecast.shortfall")

# Internal inputs (adapter outputs).

# Latest invoice version as of as_of, with terms resolved (terms_days never null)
# and the MSMED payment deadline in days.
INVOICE_IN = {
    "tenant_id": pl.String,
    "invoice_id": pl.String,
    "buyer_id": pl.String,
    "issue_date": pl.Date,
    "due_date": pl.Date,
    "terms_days": pl.Int32,
    "terms_source": pl.String,
    "msmed_deadline_days": pl.Int32,
    "gross_paise": pl.Int64,
    "gst_paise": pl.Int64,
    "expected_tds_paise": pl.Int64,
    "status": pl.String,  # "open" | "part_paid" | "paid"
}

# Timing view of the latest label as of as_of. paid_date is null when the label
# is censored (open, or a TReDS discount); censored_at is then the censoring date.
OUTCOME_IN = {
    "tenant_id": pl.String,
    "invoice_id": pl.String,
    "buyer_id": pl.String,
    "issue_date": pl.Date,
    "paid_date": pl.Date,
    "paid_fraction": pl.Float64,  # cash view: min(settled / gross, 1), TReDS counts as settled
    "censored_at": pl.Date,
}

# Buyers of the tenant with the effective sector (exchange > cin_nic > owner > unknown).
BUYER_IN = {
    "tenant_id": pl.String,
    "buyer_id": pl.String,
    "name": pl.String,
    "type": pl.String,
    "sector": pl.String,
}
