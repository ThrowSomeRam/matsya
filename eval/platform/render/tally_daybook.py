"""ledger.invoices -> a Tally-style day book (CSV), as the format is understood. NOT checked
against a real Tally export: no real file was available. Reading it back only proves that
this renderer and platform's TallyDayBookAdapter agree with each other.

One Sales voucher per invoice: the party debited with the gross, the sales ledger credited
with the taxable value, the GST split into Output CGST and SGST (same state as the supplier)
or Output IGST (another state), and a bill-wise line "New Ref <invoice no>". Known terms are
written as "Due on DD-MMM-YY" or "<n> Days", chosen per voucher; unknown terms get neither.
Dates are DD-MMM-YY; amounts are rupees with Indian digit grouping in separate Debit and
Credit columns, the party amount sometimes with a "Dr" suffix. Receipt and Payment vouchers
are interleaved as noise the reader must skip. The voucher number is Tally's own sequence,
not the invoice number. A day book shows no party GSTIN.
"""

import csv
import io

import polars as pl

from render.csv_generic import indian_rupees, issue_versions
from render.faults import rng

HEADER = ["Date", "Particulars", "Vch Type", "Vch No.", "Debit Amount", "Credit Amount"]


def _rs(paise: int) -> str:
    return indian_rupees(paise, drop_zero_decimals=False)


def _d(x) -> str:
    return x.strftime("%d-%b-%y")


def render(invoices: pl.DataFrame, invoice_no: dict[str, str], buyers: pl.DataFrame, seed: int,
           duplicate_rows: dict[str, str], supplier_gstin: str, company: str = "SUPPLIER BOOKS") -> tuple[bytes, pl.DataFrame]:
    """(day book bytes, row map: source_row (file line of the voucher) -> invoice_id, fault_id, duplicate_of_row)."""
    r = rng(seed, "tally_daybook")
    names = dict(zip(buyers["buyer_id"], buyers["name"]))
    gstins = dict(zip(buyers["buyer_id"], buyers["gstin"]))
    first = issue_versions(invoices).with_columns(
        pl.col("invoice_id").replace_strict(invoice_no, return_dtype=pl.String).alias("invoice_no")
    ).sort("issue_date", "invoice_no")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    lines = 0

    def row(cells):
        nonlocal lines
        w.writerow(cells)
        lines += 1
        return lines

    row([company])
    row(["Day Book"])
    if first.height:
        row([f"{_d(first['issue_date'].min())} to {_d(first['issue_date'].max())}"])
    row(HEADER)
    rmap, vch, noise = [], 0, 0

    def voucher(inv, bill_no, terms):
        nonlocal vch
        vch += 1
        gross, gst = inv["gross_paise"], inv["gst_paise"] or 0
        party = r.choice([_rs(gross), _rs(gross) + " Dr"])
        start = row([_d(inv["issue_date"]), names[inv["buyer_id"]], "Sales", str(vch), party, ""])
        row(["", "Sales Account", "", "", "", _rs(gross - gst)])
        if gst:
            same_state = (gstins.get(inv["buyer_id"]) or supplier_gstin)[:2] == supplier_gstin[:2]
            if same_state:
                row(["", "Output CGST", "", "", "", _rs(gst // 2)])
                row(["", "Output SGST", "", "", "", _rs(gst - gst // 2)])
            else:
                row(["", "Output IGST", "", "", "", _rs(gst)])
        row(["", f"New Ref {bill_no}{terms}", "", "", _rs(gross), ""])
        return start

    for inv in first.iter_rows(named=True):
        if not rmap or r.random() < 0.3:  # a receipt or payment between sales: not an invoice
            noise += 1
            amt = r.randrange(1_000_00, 5_00_000_00)
            kind, (dr, cr) = r.choice([("Receipt", ("HDFC Bank", names[inv["buyer_id"]])),
                                       ("Payment", ("Office Rent", "HDFC Bank"))])
            row([_d(inv["issue_date"]), dr, kind, str(noise), _rs(amt), ""])
            row(["", cr, "", "", "", _rs(amt)])
        terms = "" if inv["terms_source"] == "unknown" else r.choice(
            [f" Due on {_d(inv['due_date'])}", f" {inv['terms_days']} Days"])
        own = voucher(inv, inv["invoice_no"], terms)
        rmap.append({"source_row": own, "invoice_id": inv["invoice_id"], "fault_id": None, "duplicate_of_row": None})
        if inv["invoice_id"] in duplicate_rows:
            no = inv["invoice_no"]
            spaced = f"{no[:3]} {no[3:]}" if len(no) > 3 else no
            n = voucher(inv, spaced, terms)  # the same terms: a re-entry, not a revision
            rmap.append({"source_row": n, "invoice_id": inv["invoice_id"],
                         "fault_id": duplicate_rows[inv["invoice_id"]], "duplicate_of_row": own})
    return out.getvalue().encode("utf-8"), pl.DataFrame(
        rmap, schema={"source_row": pl.Int32, "invoice_id": pl.String, "fault_id": pl.String,
                      "duplicate_of_row": pl.Int32})
