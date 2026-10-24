"""Track B's view of the tables it reads, as Polars schemas.

Column names follow the handbook's data contracts, plus `tenant_id` on every
row. These are a stand-in until contracts v0.1 exists; an adapter will map
contract rows onto them.

Sign convention (to confirm with Track A): `amount_paise` is signed,
+ credit, - debit. `direction` is kept because the contract lists it, and
validate() checks the two agree.
"""

import polars as pl

UTC = pl.Datetime("us", "UTC")

TXN = {
    "tenant_id": pl.String,
    "txn_uid": pl.String,
    "account_id": pl.String,
    "value_date": pl.Date,
    "known_at": UTC,
    "amount_paise": pl.Int64,
    "direction": pl.String,  # "credit" | "debit"
    "narration_raw": pl.String,
    "source": pl.String,
    "dq_score": pl.Float64,
    "dup_group_id": pl.String,
}

INVOICE = {
    "tenant_id": pl.String,
    "invoice_id": pl.String,
    "irn": pl.String,
    "buyer_id": pl.String,
    "issue_date": pl.Date,
    "due_date": pl.Date,
    "terms_days": pl.Int32,
    "gross_paise": pl.Int64,
    "gst_paise": pl.Int64,
    "expected_tds_paise": pl.Int64,
    "status": pl.String,  # "open" | "part_paid" | "paid"
}

INVOICE_OUTCOME = {
    "tenant_id": pl.String,
    "invoice_id": pl.String,
    "buyer_id": pl.String,
    "issue_date": pl.Date,
    "paid_date": pl.Date,  # null while not fully paid
    "paid_fraction": pl.Float64,
    "censored_at": pl.Date,  # as-of date for open invoices, else null
}

BUYER = {
    "tenant_id": pl.String,
    "buyer_id": pl.String,
    "gstin": pl.String,
    "pan": pl.String,
    "name": pl.String,
    "type": pl.String,  # "cpse" | "listed" | "unlisted"
    "sector": pl.String,
}

BALANCE = {
    "tenant_id": pl.String,
    "account_id": pl.String,
    "date": pl.Date,
    "closing_paise": pl.Int64,
    "continuity_ok": pl.Boolean,
}

OBLIGATION = {
    "tenant_id": pl.String,
    "obligation_id": pl.String,
    "kind": pl.String,
    "due_date": pl.Date,
    "amount_paise": pl.Int64,  # null when amount_owner is the sales forecaster (GST)
    "confidence": pl.Float64,
    "amount_owner": pl.String,  # "obligations_detector" | "sales_forecaster"
}

TXN_CATEGORY = {
    "tenant_id": pl.String,
    "txn_uid": pl.String,
    "category": pl.String,  # a CATEGORIES value, or "abstain"
    "confidence": pl.Float64,
    "source": pl.String,  # "rule" | "model" | "llm" | "owner"
    "needs_llm": pl.Boolean,  # extra to the contract: abstained, waiting for the LLM
}

INVOICE_TIMING = {
    "tenant_id": pl.String,
    "invoice_id": pl.String,
    "week": pl.Int32,  # weeks from as_of, 1..horizon
    "p_paid": pl.Float64,  # P(paid in that week)
    "p_never": pl.Float64,  # P(never paid), same on every row of the invoice
    "p_beyond": pl.Float64,  # extra to the contract: alive but unpaid past the horizon
    "past_cutoff": pl.Boolean,  # extra: open longer than never_paid_cutoff_days
}

SALES_PROCESS = {
    "tenant_id": pl.String,
    "as_of": pl.Date,
    "name": pl.String,  # order_mean_per_week, order_dispersion, purchase_ratio, purchase_lag_days, buyer_share, ...
    "key": pl.String,  # buyer_id for per-buyer rows, else null
    "value": pl.Float64,
}

SCHEMAS = {
    "Txn": TXN,
    "Invoice": INVOICE,
    "InvoiceOutcome": INVOICE_OUTCOME,
    "Buyer": BUYER,
    "Balance": BALANCE,
    "Obligation": OBLIGATION,
    "TxnCategory": TXN_CATEGORY,
    "InvoiceTiming": INVOICE_TIMING,
    "SalesProcess": SALES_PROCESS,
}

NOT_NULL = {
    "Txn": ["tenant_id", "txn_uid", "account_id", "value_date", "known_at", "amount_paise", "direction"],
    "Invoice": ["tenant_id", "invoice_id", "buyer_id", "issue_date", "due_date", "gross_paise", "status"],
    "InvoiceOutcome": ["tenant_id", "invoice_id", "buyer_id", "issue_date", "paid_fraction"],
    "Buyer": ["tenant_id", "buyer_id", "type", "sector"],
    "Balance": ["tenant_id", "account_id", "date", "closing_paise"],
    "Obligation": ["tenant_id", "obligation_id", "kind", "due_date", "amount_owner"],
    "TxnCategory": ["tenant_id", "txn_uid", "category", "confidence", "source", "needs_llm"],
    "InvoiceTiming": ["tenant_id", "invoice_id", "week", "p_paid", "p_never", "p_beyond", "past_cutoff"],
    "SalesProcess": ["tenant_id", "as_of", "name", "value"],
}

ENUMS = {
    ("Txn", "direction"): {"credit", "debit"},
    ("Invoice", "status"): {"open", "part_paid", "paid"},
    ("Buyer", "type"): {"cpse", "listed", "unlisted"},
    ("TxnCategory", "source"): {"rule", "model", "llm", "owner"},
    ("Obligation", "amount_owner"): {"obligations_detector", "sales_forecaster"},
}


class SchemaError(ValueError):
    pass


def empty(name: str) -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMAS[name])


def validate(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """Raise SchemaError unless df matches the named schema exactly."""
    schema = SCHEMAS[name]
    if list(df.columns) != list(schema):
        raise SchemaError(f"{name}: columns {df.columns} != {list(schema)}")
    for col, dtype in schema.items():
        if df.schema[col] != dtype:
            raise SchemaError(f"{name}.{col}: dtype {df.schema[col]} != {dtype}")
    for col in NOT_NULL[name]:
        if df[col].null_count():
            raise SchemaError(f"{name}.{col}: has nulls")
    for (table, col), allowed in ENUMS.items():
        if table == name:
            bad = set(df[col].drop_nulls().unique()) - allowed
            if bad:
                raise SchemaError(f"{name}.{col}: unexpected values {sorted(bad)}")
    if name == "Txn":
        wrong = df.filter(
            (pl.col("amount_paise") == 0)
            | ((pl.col("amount_paise") > 0) != (pl.col("direction") == "credit"))
        )
        if wrong.height:
            raise SchemaError("Txn: amount_paise sign disagrees with direction, or is zero")
    if name == "Obligation":
        if df.filter(pl.col("amount_paise").is_null() & (pl.col("amount_owner") != "sales_forecaster")).height:
            raise SchemaError("Obligation.amount_paise: null without the sales forecaster owning it")
    if name == "InvoiceOutcome":
        if df.filter(~pl.col("paid_fraction").is_between(0.0, 1.0)).height:
            raise SchemaError("InvoiceOutcome.paid_fraction outside [0, 1]")
    return df
