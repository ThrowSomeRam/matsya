"""Foreign benchmark datasets -> contract-shaped tables, read through the normal adapters.

Benchmarks only (eval/data/SOURCES.md): nothing fitted on these tables may set a
production parameter.

Kaggle invoice payments (one tenant per business_code, buyers = cust_number):
  ledger.invoices, labels.invoice_outcomes, ledger.matches, ledger.transactions (the
  receipts), ref.buyers, ref.tenant_buyers. Issue date = baseline_create_date (the date the
  payment terms run from), due = due_in_date, paid = clear_date. There is no reporting lag
  in this data, so a payment is known on its clear date and the ledger-complete-through
  watermark is as_of itself; open labels are re-stamped at as_of with censored_at = as_of.
  Only labelled rows (isOpen = 0) are loaded: the isOpen = 1 rows are an unlabelled test
  split, not unpaid invoices.

Berka (one tenant per account): ledger.transactions with signed amounts and
ledger.balances from the bank's running balance (bank_reported, daily closing = balance
after the day's last transaction). berka_categories maps the bank's own codes to the
categoriser's classes as owner-known facts, because the categoriser is trained on Indian
narrations. For the flow forecaster and calibration benchmarks (built later) the same
berka_tables(account_id, as_of) cut serves any as_of.
"""

from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from pathlib import Path

import polars as pl
from matsya_contracts import ids
from matsya_contracts.validate import empty, schema

from matsya_intelligence.synth.world import fiscal_year, synth_tenant_id

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
UTC = pl.Datetime("us", "UTC")


def _cast(df: pl.DataFrame, table: str) -> pl.DataFrame:
    sch = schema(table)
    return df.select(list(sch)).cast(sch)


# ---------------------------------------------------------------- Kaggle


@cache
def kaggle_raw() -> pl.DataFrame:
    """Labelled, de-duplicated invoices with parsed dates. Columns: business_code, cust_number,
    name_customer, doc_id, issue_date, due_date, clear_date, amount (float), currency, terms_code."""
    d = pl.read_csv(RAW / "kaggle" / "dataset.csv", infer_schema_length=0).unique(maintain_order=True)
    d = d.filter(pl.col("isOpen") == "0").select(
        "business_code", "cust_number",
        pl.col("name_customer").str.strip_chars().replace("", None).fill_null(pl.col("cust_number")).alias("name_customer"),
        pl.col("doc_id"),
        pl.col("baseline_create_date").str.slice(0, 8).str.to_date("%Y%m%d").alias("issue_date"),
        pl.col("due_in_date").str.slice(0, 8).str.to_date("%Y%m%d").alias("due_date"),
        pl.col("clear_date").str.to_datetime("%m/%d/%Y %H:%M").dt.date().alias("clear_date"),
        pl.col("posting_date").str.to_date("%m/%d/%Y").alias("posting_date"),
        pl.col("total_open_amount").cast(pl.Float64).alias("amount"),
        pl.col("invoice_currency").alias("currency"),
        pl.col("cust_payment_terms").alias("terms_code"),
    )
    # Contract rules: due on or after issue, paid on or after issue.
    return d.filter((pl.col("due_date") >= pl.col("issue_date")) & (pl.col("clear_date") >= pl.col("issue_date")))


def kaggle_tenant_id(business_code: str) -> str:
    return synth_tenant_id(f"kaggle:{business_code}")


def kaggle_tables(raw: pl.DataFrame, business_code: str, as_of: date) -> dict[str, pl.DataFrame]:
    """Contract tables for one business_code as known at the end of as_of (UTC)."""
    tenant = kaggle_tenant_id(business_code)
    cut = datetime.combine(as_of, time(23, 59), tzinfo=timezone.utc)
    d = raw.filter((pl.col("business_code") == business_code) & (pl.col("issue_date") <= as_of)).with_columns(
        pl.lit(tenant).alias("tenant_id"),
        pl.col("cust_number").map_elements(lambda c: ids.local_buyer_id(tenant, c), return_dtype=pl.String)
        .alias("buyer_id"),
        pl.struct("doc_id", "issue_date").map_elements(
            lambda s: ids.invoice_id(tenant, "KAGGLE", s["doc_id"], fiscal_year(s["issue_date"])),
            return_dtype=pl.String).alias("invoice_id"),
        (pl.col("amount") * 100).round().cast(pl.Int64).alias("gross_paise"),
        pl.col("issue_date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC").alias("issued_at"),
        (pl.col("clear_date").cast(pl.Datetime("us")) + timedelta(hours=12)).dt.replace_time_zone("UTC").alias("paid_at"),
    ).filter(pl.col("gross_paise") > 0)
    paid = d.filter(pl.col("paid_at") <= cut)
    open_ = d.filter(pl.col("paid_at") > cut)
    paid = paid.with_columns(pl.struct("doc_id", "clear_date", "gross_paise").map_elements(
        lambda s: ids.txn_uid(tenant, "kaggle", ids.txn_key_bank_id(s["doc_id"], s["clear_date"], s["gross_paise"])),
        return_dtype=pl.String).alias("txn_uid"))

    inv_base = d.with_columns((pl.col("due_date") - pl.col("issue_date")).dt.total_days().cast(pl.Int32).alias("terms_days"),
                              pl.lit("document_terms").alias("terms_source"), pl.lit(None, pl.String).alias("irn"),
                              pl.lit(None, pl.Int64).alias("gst_paise"), pl.lit(None, pl.Int64).alias("expected_tds_paise"))
    invoices = pl.concat([
        inv_base.with_columns(pl.lit("open").alias("status"), pl.col("issued_at").alias("known_at")),
        inv_base.join(paid.select("invoice_id"), on="invoice_id").with_columns(
            pl.lit("paid").alias("status"), pl.col("paid_at").alias("known_at")),
    ])
    lab = d.select("tenant_id", "invoice_id", "buyer_id", "issue_date")
    nul = pl.lit(None, pl.Date)
    outcomes = pl.concat([
        lab.with_columns(first_paid_date=nul, paid_date=nul, paid_fraction=pl.lit(0.0),
                         settlement_kind=pl.lit(None, pl.String), censored_at=pl.col("issue_date"),
                         known_at=d["issued_at"]),
        paid.select("tenant_id", "invoice_id", "buyer_id", "issue_date").with_columns(
            first_paid_date=paid["clear_date"], paid_date=paid["clear_date"], paid_fraction=pl.lit(1.0),
            settlement_kind=pl.lit("buyer"), censored_at=nul, known_at=paid["paid_at"]),
        open_.select("tenant_id", "invoice_id", "buyer_id", "issue_date").with_columns(
            first_paid_date=nul, paid_date=nul, paid_fraction=pl.lit(0.0), settlement_kind=pl.lit(None, pl.String),
            censored_at=pl.lit(as_of), known_at=pl.lit(cut, UTC)),
    ])
    txns = paid.select("tenant_id", "txn_uid", pl.lit("kaggle").alias("account_id"), pl.col("clear_date").alias("value_date"),
                       pl.col("paid_at").alias("known_at"), pl.col("gross_paise").alias("amount_paise"),
                       pl.lit("credit").alias("direction"), (pl.lit("RECEIPT ") + pl.col("name_customer")).alias("narration_raw"),
                       pl.lit("statement_csv").alias("source"), pl.lit(1.0).alias("dq_score"),
                       pl.lit(None, pl.String).alias("dup_group_id"))
    matches = paid.select("tenant_id", "txn_uid", "invoice_id", pl.col("gross_paise").alias("allocated_paise"),
                          pl.lit(0, pl.Int64).alias("tds_inferred_paise"), pl.lit(1.0).alias("confidence"),
                          pl.lit("reference").alias("method"), pl.lit("buyer").alias("payer_kind"),
                          pl.lit(None, pl.Float64).alias("tds_rate"), pl.lit(None, pl.String).alias("tds_basis"),
                          pl.col("paid_at").alias("known_at"))
    b = d.group_by("buyer_id", maintain_order=True).agg(pl.col("name_customer").first().alias("name"))
    buyers = b.with_columns(id_basis=pl.lit("local"), pan=pl.lit(None, pl.String), gstin=pl.lit(None, pl.String),
                            type=pl.lit("unlisted"), sector=pl.lit("unknown"), sector_source=pl.lit("unknown"),
                            treds_mandated=pl.lit(None, pl.Boolean))
    tb = b.select(pl.lit(tenant).alias("tenant_id"), "buyer_id", pl.lit(None, pl.String).alias("display_name"),
                  pl.lit(None, pl.String).alias("owner_sector"), pl.lit(None, pl.Boolean).alias("written_agreement"),
                  pl.lit(None, pl.Boolean).alias("treds_onboarded"))
    return {
        "ledger.transactions": _cast(txns, "ledger.transactions"),
        "ledger.balances": empty("ledger.balances"),
        "ledger.invoices": _cast(invoices, "ledger.invoices"),
        "ledger.matches": _cast(matches, "ledger.matches"),
        "labels.invoice_outcomes": _cast(outcomes, "labels.invoice_outcomes"),
        "labels.txn_corrections": empty("labels.txn_corrections"),
        "ref.buyers": _cast(buyers, "ref.buyers"),
        "ref.tenant_buyers": _cast(tb, "ref.tenant_buyers"),
    }


# ---------------------------------------------------------------- Berka

BERKA_CATEGORY = {  # (k_symbol or operation) -> categoriser class, owner-known stand-ins
    "UVER": "emi",  # loan instalment
    "SIPO": "rent",  # household payment (rent, utilities), monthly standing order
    "POJISTNE": "professional_fee",  # insurance premium: a recurring fixed outflow
    "DUCHOD": "misc_income",  # pension
    "UROK": "misc_income",  # interest credited
    "SANKC. UROK": "bank_charges",  # penalty interest
    "SLUZBY": "bank_charges",  # statement fee
    "VYBER": "cash_withdrawal", "VYBER KARTOU": "cash_withdrawal",
    "VKLAD": "misc_income",  # cash deposit
    "PREVOD Z UCTU": "misc_income",  # transfer in from another bank
    "PREVOD NA UCET": "misc_expense",  # transfer out to another bank
}


@cache
def berka_raw() -> dict[str, pl.DataFrame]:
    r = RAW / "berka"
    tr = pl.read_csv(r / "trans.csv", infer_schema_length=0).select(
        pl.col("trans_id"), pl.col("account_id").cast(pl.Int64), pl.col("date").str.to_date().alias("value_date"),
        "type", pl.col("operation").replace("", None), pl.col("amount").cast(pl.Float64),
        pl.col("balance").cast(pl.Float64), pl.col("k_symbol").str.strip_chars().replace("", None),
        pl.col("bank").replace("", None),
    ).with_columns(
        (pl.col("amount") * 100 * pl.when(pl.col("type") == "PRIJEM").then(1).otherwise(-1)).round().cast(pl.Int64)
        .alias("amount_paise"),
        (pl.col("balance") * 100).round().cast(pl.Int64).alias("balance_paise"),
        pl.col("trans_id").cast(pl.Int64).alias("tid"),
    ).sort("account_id", "value_date", "tid")
    loan = pl.read_csv(r / "loan.csv").with_columns(pl.col("date").str.to_date())
    return {"trans": tr, "loan": loan}


def berka_tenant_id(account_id: int) -> str:
    return synth_tenant_id(f"berka:{account_id}")


def berka_tables(account_id: int, as_of: date) -> dict[str, pl.DataFrame]:
    """Contract tables for one Berka account with everything value-dated before as_of.

    No reporting lag: a transaction is known at noon UTC of its value date, the day's balance
    at the end of that day. Strictly before as_of, so a loan granted on as_of contributes
    nothing (the outside check's leakage rule).
    """
    tenant = berka_tenant_id(account_id)
    acct = str(account_id)
    t = berka_raw()["trans"].filter((pl.col("account_id") == account_id) & (pl.col("value_date") < as_of)
                                    & (pl.col("amount_paise") != 0))
    t = t.with_columns(
        pl.lit(tenant).alias("tenant_id"),
        pl.struct("trans_id", "value_date", "amount_paise").map_elements(
            lambda s: ids.txn_uid(tenant, acct, ids.txn_key_bank_id(s["trans_id"], s["value_date"], s["amount_paise"])),
            return_dtype=pl.String).alias("txn_uid"),
        (pl.col("value_date").cast(pl.Datetime("us")) + timedelta(hours=12)).dt.replace_time_zone("UTC").alias("known_at"),
    )
    txns = t.select("tenant_id", "txn_uid", pl.lit(acct).alias("account_id"), "value_date", "known_at", "amount_paise",
                    pl.when(pl.col("amount_paise") > 0).then(pl.lit("credit")).otherwise(pl.lit("debit")).alias("direction"),
                    pl.concat_str([pl.col("operation").fill_null(""), pl.col("k_symbol").fill_null(""),
                                   pl.col("bank").fill_null("")], separator=" ").str.strip_chars().alias("narration_raw"),
                    pl.lit("statement_csv").alias("source"), pl.lit(1.0).alias("dq_score"),
                    pl.lit(None, pl.String).alias("dup_group_id"))
    bal = t.group_by("value_date", maintain_order=True).agg(pl.col("balance_paise").last()).select(
        pl.lit(tenant).alias("tenant_id"), pl.lit(acct).alias("account_id"), pl.col("value_date").alias("date"),
        pl.col("balance_paise").alias("closing_paise"), pl.lit(True).alias("continuity_ok"),
        pl.lit("bank_reported").alias("balance_source"),
        (pl.col("value_date").cast(pl.Datetime("us")) + timedelta(hours=23, minutes=59)).dt.replace_time_zone("UTC")
        .alias("known_at"))
    return {
        "ledger.transactions": _cast(txns, "ledger.transactions"),
        "ledger.balances": _cast(bal, "ledger.balances"),
        "ledger.invoices": empty("ledger.invoices"), "ledger.matches": empty("ledger.matches"),
        "labels.invoice_outcomes": empty("labels.invoice_outcomes"),
        "labels.txn_corrections": empty("labels.txn_corrections"),
        "ref.buyers": empty("ref.buyers"), "ref.tenant_buyers": empty("ref.tenant_buyers"),
    }, t


def berka_categories(raw_rows: pl.DataFrame) -> pl.DataFrame:
    """features.txn_category-shaped (txn_uid, category) from the bank's own codes."""
    key = pl.coalesce(pl.col("k_symbol").replace_strict(BERKA_CATEGORY, default=None),
                      pl.col("operation").replace_strict(BERKA_CATEGORY, default=None))
    fallback = pl.when(pl.col("amount_paise") > 0).then(pl.lit("misc_income")).otherwise(pl.lit("misc_expense"))
    return raw_rows.select("txn_uid", pl.coalesce(key, fallback).alias("category"))
