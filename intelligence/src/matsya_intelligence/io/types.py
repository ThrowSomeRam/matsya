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
    "amount_paise": pl.Int64,
    "confidence": pl.Float64,
}

SCHEMAS = {
    "Txn": TXN,
    "Invoice": INVOICE,
    "InvoiceOutcome": INVOICE_OUTCOME,
    "Buyer": BUYER,
    "Balance": BALANCE,
    "Obligation": OBLIGATION,
}

NOT_NULL = {
    "Txn": ["tenant_id", "txn_uid", "account_id", "value_date", "known_at", "amount_paise", "direction"],
    "Invoice": ["tenant_id", "invoice_id", "buyer_id", "issue_date", "due_date", "gross_paise", "status"],
    "InvoiceOutcome": ["tenant_id", "invoice_id", "buyer_id", "issue_date", "paid_fraction"],
    "Buyer": ["tenant_id", "buyer_id", "type", "sector"],
    "Balance": ["tenant_id", "account_id", "date", "closing_paise"],
    "Obligation": ["tenant_id", "obligation_id", "kind", "due_date", "amount_paise"],
}

ENUMS = {
    ("Txn", "direction"): {"credit", "debit"},
    ("Invoice", "status"): {"open", "part_paid", "paid"},
    ("Buyer", "type"): {"cpse", "listed", "unlisted"},
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
    if name == "InvoiceOutcome":
        if df.filter(~pl.col("paid_fraction").is_between(0.0, 1.0)).height:
            raise SchemaError("InvoiceOutcome.paid_fraction outside [0, 1]")
    return df
