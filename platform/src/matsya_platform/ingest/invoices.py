"""Invoice ingest core: format-neutral lines -> ledger.invoices rows.

A format adapter (registry.ADAPTERS: generic CSV, Tally day book, GST e-invoice JSON) turns
a file into InvoiceLine records and per-record rejects. Everything after that lives here and
is the same for every format: invoice_id, fiscal year, terms, buyer resolution, the supplier
check, duplicates and validation against the contract.

Rules:
- invoice_id = contracts ids.invoice_id(tenant, supplier GSTIN, invoice no, FY of issue).
- Terms: stated credit days or a due date give terms_source document_terms. Neither gives
  terms_source unknown, terms_days null and due_date = issue + UNKNOWN_TERMS_DAYS.
  A stated due date wins over stated credit days when both are present.
- Buyer: a GSTIN resolves through its PAN; a missing or unknown GSTIN falls back to the
  buyer's name (exact after normalisation, then a unique fuzzy match). No match rejects the
  line; ingest never invents a buyer.
- A second line with the same invoice_id in one file is a duplicate and is dropped; an
  invoice_id already in the ledger is skipped.
- A line that names its seller (e-invoice SellerDtls) must name this tenant's supplier
  GSTIN, or it is rejected: invoice_id is hashed with the supplier GSTIN, so a foreign
  invoice would otherwise get a valid-looking id under the wrong supplier.
- Ingest writes the issue-time version only: status open, irn as the source states it (only
  e-invoices carry one), expected_tds_paise null (TDS inference belongs to the matcher).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

import polars as pl
from pydantic import ValidationError

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.enums import InvoiceStatus, TermsSource
from matsya_contracts.validate import frame

UNKNOWN_TERMS_DAYS = 45  # VERIFY: MSMED Act s.15 maximum, used when no terms are stated
NAME_SIMILARITY = 0.8

RejectReason = Literal["missing_field", "bad_amount", "bad_date", "total_mismatch", "unresolved_buyer",
                       "ambiguous_buyer", "invalid_row", "malformed", "unsupported_document", "wrong_supplier"]
Method = Literal["gstin", "name", "name_fuzzy"]


@dataclass(frozen=True)
class InvoiceLine:
    # 1-based position of the record in its file, in the adapter's own unit: CSV data row,
    # day book line where the voucher starts, e-invoice document index.
    source_row: int
    invoice_no: str
    issue_date: date
    buyer_name: str | None
    buyer_gstin: str | None
    gross_paise: int
    gst_paise: int | None
    taxable_paise: int | None
    credit_days: int | None
    due_date: date | None
    # Charges in the gross that are neither taxable value nor GST: round off, other charges.
    # The total check is taxable + GST + other = gross.
    other_paise: int = 0
    irn: str | None = None  # e-invoice reference number; no other source has one
    seller_gstin: str | None = None  # when the source states the seller (e-invoice); checked


@dataclass(frozen=True)
class Reject:
    source_row: int
    reason: RejectReason
    detail: str


class InvoiceAdapter(Protocol):
    name: str

    def parse(self, data: bytes) -> tuple[list[InvoiceLine], list[Reject]]: ...


def fiscal_year(d: date) -> str:
    """Indian FY, April to March: 2025-04-01 -> "2025-26"."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^A-Z0-9]+", ids.normalise_party(text)) if t}


class BuyerDirectory:
    """Buyers this tenant can invoice: ref.buyers, plus the tenant's own names for them."""

    def __init__(self, buyers: pl.DataFrame, tenant_buyers: pl.DataFrame | None, tenant_id: str):
        self.by_pan = {p: b for p, b in zip(buyers["pan"], buyers["buyer_id"]) if p}
        names: dict[str, set[str]] = {}
        for name, b in zip(buyers["name"], buyers["buyer_id"]):
            names.setdefault(ids.normalise_party(name), set()).add(b)
        if tenant_buyers is not None:
            mine = tenant_buyers.filter(pl.col("tenant_id") == tenant_id)
            for name, b in zip(mine["display_name"], mine["buyer_id"]):
                if name:
                    names.setdefault(ids.normalise_party(name), set()).add(b)
        self.by_name = names

    def resolve(self, gstin: str | None, name: str | None) -> tuple[str | None, Method | None, str]:
        if gstin:
            b = self.by_pan.get(ids.pan_of_gstin(gstin.strip().upper()))
            if b:
                return b, "gstin", ""
        if not name:
            return None, None, "no GSTIN match and no name"
        exact = self.by_name.get(ids.normalise_party(name), set())
        if len(exact) == 1:
            return next(iter(exact)), "name", ""
        if len(exact) > 1:
            return None, None, f"name matches {len(exact)} buyers"
        want = _tokens(name)
        scored = []
        for known, bs in self.by_name.items():
            have = _tokens(known)
            sim = len(want & have) / len(want | have) if want | have else 0.0
            if sim >= NAME_SIMILARITY:
                scored.append((sim, bs))
        if not scored:
            return None, None, "no buyer with this GSTIN or name"
        best = max(s for s, _ in scored)
        top = set().union(*(bs for s, bs in scored if s == best))
        if len(top) == 1:
            return next(iter(top)), "name_fuzzy", f"similarity {best:.2f}"
        return None, None, f"fuzzy name matches {len(top)} buyers"


@dataclass(frozen=True)
class Resolution:
    source_row: int
    invoice_id: str
    buyer_id: str
    method: Method


@dataclass(frozen=True)
class Duplicate:
    source_row: int
    invoice_id: str
    first_row: int | None  # None: already in the ledger before this file
    identical: bool  # same facts once whitespace and case are normalised


@dataclass(frozen=True)
class IngestResult:
    invoices: pl.DataFrame  # new ledger.invoices rows
    rejects: list[Reject] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)


def _facts(line: InvoiceLine) -> tuple:
    return (ids.normalise_invoice_no(line.invoice_no), line.issue_date, ids.normalise_party(line.buyer_name or ""),
            (line.buyer_gstin or "").strip().upper(), line.gross_paise, line.gst_paise, line.taxable_paise,
            line.credit_days, line.due_date, line.other_paise, line.irn)


def _terms(line: InvoiceLine) -> tuple[int | None, date, TermsSource]:
    if line.due_date is not None:
        return (line.due_date - line.issue_date).days, line.due_date, TermsSource.DOCUMENT_TERMS
    if line.credit_days is not None:
        return line.credit_days, line.issue_date + timedelta(days=line.credit_days), TermsSource.DOCUMENT_TERMS
    return None, line.issue_date + timedelta(days=UNKNOWN_TERMS_DAYS), TermsSource.UNKNOWN


def ingest(data: bytes, adapter: InvoiceAdapter, *, tenant_id: str, supplier_gstin: str, received_at: datetime,
           buyers: BuyerDirectory, existing_invoice_ids: frozenset[str] = frozenset()) -> IngestResult:
    lines, rejects = adapter.parse(data)
    rows, resolutions, duplicates = [], [], []
    first: dict[str, InvoiceLine] = {}
    for line in lines:
        if line.seller_gstin is not None and line.seller_gstin.strip().upper() != supplier_gstin.upper():
            rejects.append(Reject(line.source_row, "wrong_supplier",
                                  f"seller {line.seller_gstin} is not this tenant's supplier {supplier_gstin}"))
            continue
        inv_id = ids.invoice_id(tenant_id, supplier_gstin, line.invoice_no, fiscal_year(line.issue_date))
        if inv_id in first:
            duplicates.append(Duplicate(line.source_row, inv_id, first[inv_id].source_row,
                                        _facts(line) == _facts(first[inv_id])))
            continue
        if inv_id in existing_invoice_ids:
            duplicates.append(Duplicate(line.source_row, inv_id, None, False))
            continue
        if line.taxable_paise is not None and line.gst_paise is not None \
                and line.taxable_paise + line.gst_paise + line.other_paise != line.gross_paise:
            rejects.append(Reject(line.source_row, "total_mismatch",
                                  f"{line.taxable_paise} + {line.gst_paise} + {line.other_paise} != {line.gross_paise}"))
            continue
        buyer_id, method, why = buyers.resolve(line.buyer_gstin, line.buyer_name)
        if buyer_id is None:
            reason = "ambiguous_buyer" if "matches" in why else "unresolved_buyer"
            rejects.append(Reject(line.source_row, reason, why))
            continue
        terms_days, due, source = _terms(line)
        try:
            row = T.Invoice(tenant_id=tenant_id, invoice_id=inv_id, irn=line.irn, buyer_id=buyer_id,
                            issue_date=line.issue_date, due_date=due, terms_days=terms_days, terms_source=source,
                            gross_paise=line.gross_paise, gst_paise=line.gst_paise, expected_tds_paise=None,
                            status=InvoiceStatus.OPEN, known_at=received_at)
        except ValidationError as e:
            rejects.append(Reject(line.source_row, "invalid_row", str(e).splitlines()[0]))
            continue
        first[inv_id] = line
        rows.append(row)
        resolutions.append(Resolution(line.source_row, inv_id, buyer_id, method))
    return IngestResult(invoices=frame(T.Invoice, rows), rejects=rejects, resolutions=resolutions,
                        duplicates=duplicates)
