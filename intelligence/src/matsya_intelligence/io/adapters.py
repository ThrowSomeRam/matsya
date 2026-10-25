"""Contract tables -> internal inputs for each component. Models never read contract columns.

As-of rule: append-only tables (ledger.balances, ledger.invoices, ledger.matches,
labels.invoice_outcomes) are read per key as the latest row with known_at <= as_of;
ledger.transactions keeps rows with known_at <= as_of. Nothing reported after as_of
is used, so there is no separate label lag.

Rules applied here (docs/track-b-decisions.md, contract positions):
- TReDS: an invoice with an as-of match whose payer_kind is treds_financier is censored
  for timing at the discount date (the earliest such matched credit's value_date).
  Its cash is real: the credit stays in the ledger and the invoice counts as settled.
- Terms: terms_source unknown -> terms and MSMED deadline 45 days (constant
  msmed_act.section_15.unknown_terms_default_days); default_15d -> 15 days;
  written_agreement and document_terms -> the stated terms, deadline capped at 45.
- Watermark: payment-timing labels and engine survival are cut at the ledger-complete-
  through date (ledger_complete_through), not at as_of, so payments value-dated before
  as_of but not yet reported do not read as unpaid weeks.
- Sector: tables.effective_sector precedence, exchange > cin_nic > owner > unknown.
- Unknown enum values map to a fallback rather than fail (enums are additive only).
"""

from datetime import date, datetime, time, timezone

import polars as pl
from matsya_contracts.enums import PayerKind, TermsSource

from matsya_intelligence.io.types import BUYER_IN, INVOICE_IN, OUTCOME_IN
from matsya_intelligence.statutory import gst_rate as standard_gst_rate
from matsya_intelligence.statutory import s15_days
from matsya_intelligence.synth.world import load_constants


def as_of_cutoff(as_of: date | datetime) -> datetime:
    """The one place a date becomes a read cutoff: a date means the end of that day, UTC.
    VERIFY: UTC or IST business day (question 8 to Track A). A datetime passes unchanged,
    so callers should pass the real fetch time whenever they have it."""
    if isinstance(as_of, datetime):
        return as_of
    return datetime.combine(as_of, time(23, 59, 59, 999999), tzinfo=timezone.utc)


def as_of_date(as_of: date | datetime) -> date:
    """The calendar day of as_of, for day arithmetic (never for filtering rows)."""
    return as_of.date() if isinstance(as_of, datetime) else as_of


def as_of_read(df: pl.DataFrame, key: list[str], as_of: date | datetime) -> pl.DataFrame:
    """Per key, the latest row with known_at <= as_of."""
    return (df.filter(pl.col("known_at") <= as_of_cutoff(as_of))
            .sort("known_at").group_by(key, maintain_order=True).last())


def transactions(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> pl.DataFrame:
    """ledger.transactions known by as_of, in contract shape (the categoriser's input)."""
    return tables["ledger.transactions"].filter(pl.col("known_at") <= as_of_cutoff(as_of)).sort("value_date", "txn_uid")


def opening_balance(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> tuple[date, int, str]:
    """(date, closing_paise summed over accounts, balance_source) as known by as_of.

    Each account contributes its own latest balance date <= as_of, so an account last
    updated earlier than another still counts. The returned date is the earliest of those
    (the figure is no fresher than its stalest account). Contract as-of read per (tenant,
    account, date): the latest known_at wins whatever its balance_source; a bank_reported
    row known at fetch time includes transactions not yet reported to us.
    """
    d = as_of_date(as_of)
    b = as_of_read(tables["ledger.balances"], ["tenant_id", "account_id", "date"], as_of).filter(pl.col("date") <= d)
    if b.is_empty():
        raise ValueError("no balance known by as_of")
    rows = b.sort("date").group_by("tenant_id", "account_id", maintain_order=True).last()
    srcs = set(rows["balance_source"])
    src = srcs.pop() if len(srcs) == 1 else "mixed"
    return rows["date"].min(), int(rows["closing_paise"].sum()), src


def known_balances(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> dict[date, int]:
    """Closing balance per date known at as_of (as-of read, summed over accounts): the outcomes
    the calibration component scores its bands against."""
    b = as_of_read(tables["ledger.balances"], ["tenant_id", "account_id", "date"], as_of)
    b = b.filter(pl.col("date") <= as_of_date(as_of)).group_by("date").agg(pl.col("closing_paise").sum())
    return dict(zip(b["date"], b["closing_paise"]))


def resolve_terms(terms_source: str, terms_days: int | None) -> tuple[int, int]:
    """(terms_days, MSMED deadline days) from the contract's terms fields."""
    cap, no_agreement = s15_days()
    unknown = int(load_constants()["msmed_act"]["section_15"]["unknown_terms_default_days"]["value"])
    if terms_source == TermsSource.DEFAULT_15D.value:
        return (terms_days if terms_days is not None else no_agreement), no_agreement
    if terms_source in (TermsSource.WRITTEN_AGREEMENT.value, TermsSource.DOCUMENT_TERMS.value) and terms_days is not None:
        return terms_days, min(terms_days, cap)
    return unknown, unknown  # unknown, an unseen enum member, or stated terms missing


def invoices(tables: dict[str, pl.DataFrame], as_of: date | datetime, gst_rate: float | None = None) -> pl.DataFrame:
    gst_rate = standard_gst_rate() if gst_rate is None else gst_rate
    inv = as_of_read(tables["ledger.invoices"], ["tenant_id", "invoice_id"], as_of)
    resolved = [resolve_terms(s, t) for s, t in zip(inv["terms_source"], inv["terms_days"])]
    terms = pl.Series([r[0] for r in resolved], dtype=pl.Int32)
    deadline = pl.Series([r[1] for r in resolved], dtype=pl.Int32)
    settled = treds_discounts(tables, as_of)
    return inv.with_columns(
        terms.alias("terms_days"), deadline.alias("msmed_deadline_days"),
        pl.when(pl.col("terms_source") == TermsSource.UNKNOWN.value)
        .then(pl.col("issue_date") + pl.duration(days=terms)).otherwise(pl.col("due_date")).alias("due_date"),
        pl.col("gst_paise").fill_null((pl.col("gross_paise") * gst_rate / (1 + gst_rate)).round().cast(pl.Int64)),
        pl.col("expected_tds_paise").fill_null(0),
        pl.when(pl.col("invoice_id").is_in(settled["invoice_id"].implode())).then(pl.lit("paid"))
        .otherwise(pl.col("status")).alias("status"),
    ).select(list(INVOICE_IN)).cast(INVOICE_IN).sort("issue_date", "invoice_id")


def treds_discounts(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> pl.DataFrame:
    """invoice_id, discount_date: invoices settled by a TReDS financier, as known by as_of."""
    m = as_of_read(tables["ledger.matches"], ["tenant_id", "txn_uid", "invoice_id"], as_of)
    m = m.filter(pl.col("payer_kind") == PayerKind.TREDS_FINANCIER.value)
    txns = transactions(tables, as_of).select("txn_uid", "value_date")
    return (m.join(txns, on="txn_uid").group_by("invoice_id").agg(pl.col("value_date").min().alias("discount_date"))
            .sort("invoice_id"))


def outcomes(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> pl.DataFrame:
    """Timing labels as of as_of: censored_at wins over paid_date (tables.timing_state), and a
    treds_financier match censors at the discount date even if the label says otherwise."""
    out = as_of_read(tables["labels.invoice_outcomes"], ["tenant_id", "invoice_id"], as_of)
    disc = treds_discounts(tables, as_of)
    out = out.join(disc, on="invoice_id", how="left").with_columns(
        pl.min_horizontal(pl.col("censored_at"), pl.col("discount_date")).alias("censored_at"))
    return out.with_columns(
        pl.when(pl.col("censored_at").is_null()).then(pl.col("paid_date")).alias("paid_date"),
        pl.when(pl.col("discount_date").is_not_null()).then(pl.lit(1.0)).otherwise(pl.col("paid_fraction"))
        .alias("paid_fraction"),
    ).select(list(OUTCOME_IN)).cast(OUTCOME_IN).sort("issue_date", "invoice_id")


def ledger_complete_through(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> date:
    """The ledger-complete-through watermark, read back from the labels: open labels are
    re-stamped at each fetch with censored_at = watermark, so it is their latest censored_at.
    With no open label (or no watermark from Track A yet), as_of itself."""
    d = as_of_date(as_of)
    out = as_of_read(tables["labels.invoice_outcomes"], ["tenant_id", "invoice_id"], as_of)
    w = out.filter(pl.col("paid_date").is_null())["censored_at"].max()
    return min(w, d) if w is not None else d


def buyers(tables: dict[str, pl.DataFrame], tenant_id: str) -> pl.DataFrame:
    tb = tables["ref.tenant_buyers"].filter(pl.col("tenant_id") == tenant_id)
    b = tables["ref.buyers"].join(tb.select("buyer_id", "owner_sector"), on="buyer_id")
    sector = (pl.when(pl.col("sector_source") != "unknown").then(pl.col("sector"))
              .when(pl.col("owner_sector").is_not_null() & (pl.col("owner_sector") != "unknown"))
              .then(pl.col("owner_sector")).otherwise(pl.lit("unknown")))
    return b.select(pl.lit(tenant_id).alias("tenant_id"), "buyer_id", "name", "type", sector.alias("sector")) \
        .cast(BUYER_IN).sort("buyer_id")


def track_b_inputs(tables: dict[str, pl.DataFrame], as_of: date | datetime, gst_rate: float | None = None) -> dict:
    """Every internal input, from contract tables for one tenant."""
    txns = transactions(tables, as_of)
    tenant = txns["tenant_id"][0]
    bal_date, bal, src = opening_balance(tables, as_of)
    return {
        "tenant_id": tenant,
        "transactions": txns,
        "invoices": invoices(tables, as_of, gst_rate),
        "invoice_outcomes": outcomes(tables, as_of),
        "buyers": buyers(tables, tenant),
        "opening_balance": {"date": bal_date, "closing_paise": bal, "balance_source": src},
        "complete_through": ledger_complete_through(tables, as_of),
    }


def for_tenant(tables: dict[str, pl.DataFrame], tenant_id: str) -> dict[str, pl.DataFrame]:
    """Restrict tenant-scoped tables to one tenant; global ref.* tables pass through."""
    return {k: (v.filter(pl.col("tenant_id") == tenant_id) if "tenant_id" in v.columns else v)
            for k, v in tables.items()}


def corrections(tables: dict[str, pl.DataFrame], as_of: date | datetime) -> pl.DataFrame:
    """labels.txn_corrections -> the categoriser's overrides (tenant_id, txn_uid, category).
    Latest corrected_at wins; corrections made after as_of are not used."""
    c = tables.get("labels.txn_corrections")
    if c is None or c.is_empty():
        return pl.DataFrame(schema={"tenant_id": pl.String, "txn_uid": pl.String, "category": pl.String})
    return (c.filter(pl.col("corrected_at") <= as_of_cutoff(as_of)).sort("corrected_at")
            .group_by("tenant_id", "txn_uid", maintain_order=True).last().select("tenant_id", "txn_uid", "category"))
