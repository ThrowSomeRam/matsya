"""Tally-style day book adapter. NOT verified against a real Tally export: no real file was
available. The layout below is the format as understood, and the round trip in
eval/platform only proves that our renderer and this reader agree with each other.

Assumed layout (CSV, as a day book exported to a spreadsheet):
- a few title lines (company name, "Day Book", the period), then a header row containing
  Date, Particulars, Vch Type, Vch No., Debit Amount, Credit Amount;
- each voucher starts on a line with a date (DD-MMM-YY, e.g. 07-Apr-25) and the party or
  first ledger in Particulars; the lines after it, with no date, are its other ledger lines;
- amounts are rupees with Indian digit grouping, in separate Debit and Credit columns rather
  than a sign; a Dr or Cr suffix, if present, must agree with the column it sits in;
- a Sales voucher debits the party (the gross), credits the sales ledger (taxable value)
  and the GST ledgers (Output CGST / SGST / IGST / Cess), and any other ledger (round off,
  freight) goes to other_paise, debit or credit; it carries bill-wise details as
  "New Ref <bill name>", optionally followed by "Due on DD-MMM-YY" or "<n> Days". The bill
  name is the invoice reference; the voucher number is Tally's own sequence and is not used.

Only Sales vouchers are invoices; every other voucher type is skipped, not rejected. A day
book carries no party GSTIN, so every buyer resolves by name.
"""

import csv
import io
import re
from datetime import date, datetime

from matsya_platform.ingest.fields import FieldError, parse_rupees
from matsya_platform.ingest.invoices import InvoiceLine, Reject

HEADER = ("date", "particulars", "vch type", "vch no.", "debit amount", "credit amount")
_GST_LEDGER = re.compile(r"\b(C|S|I|U)?GST\b|\bCESS\b", re.IGNORECASE)
_BILL = re.compile(r"^\s*(New Ref|Agst Ref|Advance|On Account)\s+(.+?)(?:\s+Due on\s+(\S+)|\s+(\d+)\s+Days?)?\s*$",
                   re.IGNORECASE)
_DRCR = re.compile(r"\s*(Dr|Cr)\.?\s*$", re.IGNORECASE)


def parse_tally_date(text: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%d-%b-%y").date()
    except ValueError:
        raise FieldError(f"bad date {text!r}") from None


def _amount(text: str, column: str) -> int | None:
    s = text.strip()
    if not s:
        return None
    m = _DRCR.search(s)
    if m:
        if m.group(1).lower() != {"debit": "dr", "credit": "cr"}[column]:
            raise FieldError(f"bad amount {text!r}: {m.group(1)} in the {column} column")
        s = s[:m.start()]
    return parse_rupees(s)


class TallyDayBookAdapter:
    name = "tally_daybook"

    def parse(self, data: bytes) -> tuple[list[InvoiceLine], list[Reject]]:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        head = next((i for i, r in enumerate(rows) if tuple(c.strip().lower() for c in r[:6]) == HEADER), None)
        if head is None:
            raise FieldError("day book has no Date / Particulars / Vch Type / Vch No. / Debit / Credit header")
        vouchers: list[tuple[int, list[list[str]]]] = []
        for n, r in enumerate(rows[head + 1:], start=head + 2):  # n: 1-based file line
            r = (r + [""] * 6)[:6]
            if not any(c.strip() for c in r):
                continue
            if r[0].strip():
                vouchers.append((n, [r]))
            elif vouchers:
                vouchers[-1][1].append(r)
        lines, rejects = [], []
        for n, vrows in vouchers:
            if vrows[0][2].strip().lower() != "sales":
                continue
            try:
                lines.append(self._sales(n, vrows))
            except FieldError as e:
                reason = ("bad_date" if "date" in str(e) else "bad_amount" if "amount" in str(e)
                          else "total_mismatch" if "balance" in str(e) else "missing_field")
                rejects.append(Reject(n, reason, str(e)))
        return lines, rejects

    def _sales(self, n: int, vrows: list[list[str]]) -> InvoiceLine:
        first = vrows[0]
        issue = parse_tally_date(first[0])
        party = first[1].strip()
        gross = _amount(first[4], "debit")
        if not party or gross is None:
            raise FieldError("sales voucher has no party debit")
        debit, credit, taxable, gst, other, bill, due, days = gross, 0, 0, 0, 0, None, None, None
        for r in vrows[1:]:
            name = r[1].strip()
            m = _BILL.match(name)
            if m:  # bill-wise detail: a reference against the party amount, not a ledger line
                if bill is not None:
                    raise FieldError("more than one bill-wise reference; split bills are not supported")
                bill = m.group(2).strip()
                due = parse_tally_date(m.group(3)) if m.group(3) else None
                days = int(m.group(4)) if m.group(4) else None
                continue
            dr, cr = _amount(r[4], "debit"), _amount(r[5], "credit")
            debit += dr or 0
            credit += cr or 0
            if cr is not None:
                if _GST_LEDGER.search(name):
                    gst += cr
                elif "sales" in name.lower():
                    taxable += cr
                else:  # round off, freight: in the gross, neither taxable value nor GST
                    other += cr
            if dr is not None:
                other -= dr  # a debited round off reduces the gross
        if debit != credit:
            raise FieldError(f"voucher does not balance: debit {debit} != credit {credit}")
        if not bill:
            raise FieldError("sales voucher has no bill-wise reference (the invoice number)")
        return InvoiceLine(source_row=n, invoice_no=bill, issue_date=issue, buyer_name=party, buyer_gstin=None,
                           gross_paise=gross, gst_paise=gst, taxable_paise=taxable, other_paise=other,
                           credit_days=days, due_date=due)
