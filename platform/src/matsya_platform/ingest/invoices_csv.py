"""Generic owner-style invoice CSV adapter.

Copes with what owners export by hand: a UTF-8 BOM, header names in any case and spacing
(matched through HEADERS), blank and trailing blank rows, dates in several day-first
formats, and rupee amounts with Indian or Western comma grouping, an optional Rs/INR/rupee
sign and zero, one or two decimals. Amounts become integer paise exactly; negative amounts
(credit notes) are rejected until they have their own path.
"""

import csv
import io
import re

from matsya_platform.ingest.fields import FieldError, parse_date, parse_rupees  # noqa: F401 (re-exported)
from matsya_platform.ingest.invoices import InvoiceLine, Reject

HEADERS = {
    "invoice_no": ("invoice no", "invoice number", "invoice #", "inv no", "bill no"),
    "issue_date": ("invoice date", "date", "bill date"),
    "buyer_name": ("customer name", "customer", "party name", "buyer"),
    "buyer_gstin": ("customer gstin", "gstin", "party gstin", "buyer gstin"),
    "credit_days": ("credit days", "credit period", "terms days"),
    "due_date": ("due date",),
    "taxable": ("taxable value", "taxable amount"),
    "gst": ("gst amount", "gst", "tax amount"),
    "gross": ("invoice total", "total", "invoice amount", "amount"),
}
def _header_map(header: list[str]) -> dict[str, int]:
    norm = [re.sub(r"\s+", " ", h.strip().lower()) for h in header]
    out = {}
    for field_, names in HEADERS.items():
        for i, h in enumerate(norm):
            if h in names:
                out[field_] = i
                break
    return out


class GenericCsvAdapter:
    name = "csv_generic"

    def parse(self, data: bytes) -> tuple[list[InvoiceLine], list[Reject]]:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
        rows = list(reader)
        if not rows:
            return [], []
        cols = _header_map(rows[0])
        missing = {"invoice_no", "issue_date", "gross"} - set(cols)
        if missing:
            raise FieldError(f"invoice CSV lacks columns {sorted(missing)}")
        lines, rejects = [], []
        for n, row in enumerate(rows[1:], start=1):
            if not any(c.strip() for c in row):
                continue

            def cell(f: str) -> str:
                i = cols.get(f)
                return row[i].strip() if i is not None and i < len(row) else ""

            try:
                if not cell("invoice_no"):
                    raise FieldError("missing invoice number")
                credit = cell("credit_days")
                m = re.search(r"\d+", credit)
                lines.append(InvoiceLine(
                    source_row=n, invoice_no=cell("invoice_no"), issue_date=parse_date(cell("issue_date")),
                    buyer_name=cell("buyer_name") or None, buyer_gstin=cell("buyer_gstin") or None,
                    gross_paise=parse_rupees(cell("gross")),
                    gst_paise=parse_rupees(cell("gst")) if cell("gst") else None,
                    taxable_paise=parse_rupees(cell("taxable")) if cell("taxable") else None,
                    credit_days=int(m.group()) if m else None,
                    due_date=parse_date(cell("due_date")) if cell("due_date") else None,
                ))
            except FieldError as e:
                reason = "bad_date" if "date" in str(e) else "bad_amount" if "amount" in str(e) else "missing_field"
                rejects.append(Reject(n, reason, str(e)))
        return lines, rejects
