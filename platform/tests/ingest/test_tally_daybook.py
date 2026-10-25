"""The Tally day book adapter against a hand-written day book. The layout is the format as
understood, not a real export (see the adapter's docstring)."""

from datetime import date, timedelta

import pytest
from conftest import TENANT
from test_invoices_csv import RECEIVED, SUPPLIER, buyers, inv_id

from matsya_contracts import ids
from matsya_platform.ingest.fields import FieldError
from matsya_platform.ingest.invoices import UNKNOWN_TERMS_DAYS, ingest
from matsya_platform.ingest.tally_daybook import TallyDayBookAdapter, parse_tally_date

DAYBOOK = """Sharma Fabrics Pvt Ltd
Day Book
1-Apr-25 to 31-Mar-26
Date,Particulars,Vch Type,Vch No.,Debit Amount,Credit Amount
07-Apr-25,ACME TEXTILES LTD,Sales,1,"1,18,000.50",
,Sales Account,,,,"1,00,000.50"
,Output CGST,,,,"9,000.00"
,Output SGST,,,,"9,000.00"
,New Ref INV-001 Due on 07-May-25,,,"1,18,000.50 Dr",
08-Apr-25,HDFC Bank,Receipt,1,"50,000.00",
,ACME TEXTILES LTD,,,,"50,000.00"
09-Apr-25,Beta Foods Pvt Ltd,Sales,2,"59,000.00 Dr",
,Sales Account,,,,"50,000.00"
,Output IGST,,,,"9,000.00 Cr"
,New Ref INV-002 30 Days,,,"59,000.00",
10-Apr-25,ACME TEXTILES LTD,Sales,3,"11,800.00",
,Sales Account,,,,"10,000.00"
,Output IGST,,,,"1,800.00"
,New Ref INV-003,,,"11,800.00",
11-Apr-25,ACME TEXTILES LTD,Sales,4,"11,800.00",
,Sales Account,,,,"10,000.00"
,Output IGST,,,,"1,700.00"
,New Ref INV-004,,,"11,800.00",
12-Apr-25,ACME TEXTILES LTD,Sales,5,"10,000.40",
,Sales Account,,,,"8,474.92"
,Output IGST,,,,"1,525.49"
,Round Off,,,0.01,
,New Ref INV-005 Due on 12-May-25,,,"10,000.40",
13-Apr-25,ACME TEXTILES LTD,Sales,6,"1,000.00 Cr",
,Sales Account,,,,"1,000.00"
,New Ref INV-006,,,"1,000.00",
32-Apr-25,ACME TEXTILES LTD,Sales,7,"1,000.00",
,Sales Account,,,,"1,000.00"
,New Ref INV-007,,,"1,000.00",
14-Apr-25,ACME TEXTILES LTD,Sales,8,"1,000.00",
,Sales Account,,,,"1,000.00"
15-Apr-25,Office Rent,Payment,2,"20,000.00",
,HDFC Bank,,,,"20,000.00"

"""


def run():
    return ingest(DAYBOOK.encode(), TallyDayBookAdapter(), tenant_id=TENANT, supplier_gstin=SUPPLIER,
                  received_at=RECEIVED, buyers=buyers())


def by_no(r, no, d):
    rows = r.invoices.filter(r.invoices["invoice_id"] == inv_id(no, d))
    assert rows.height == 1, no
    return rows.row(0, named=True)


def test_sales_vouchers_become_invoices_and_others_are_skipped():
    r = run()
    assert r.invoices.height == 4  # INV-001, 002, 003, 005; receipts and payments are not invoices
    a = by_no(r, "INV-001", date(2025, 4, 7))
    assert (a["gross_paise"], a["gst_paise"], a["terms_days"], a["due_date"], a["terms_source"]) == \
        (11800050, 1800000, 30, date(2025, 5, 7), "document_terms")
    assert a["irn"] is None


def test_credit_period_in_days_and_missing_terms():
    r = run()
    b = by_no(r, "INV-002", date(2025, 4, 9))
    assert (b["terms_days"], b["terms_source"]) == (30, "document_terms")
    c = by_no(r, "INV-003", date(2025, 4, 10))
    assert (c["terms_days"], c["terms_source"], c["due_date"]) == \
        (None, "unknown", date(2025, 4, 10) + timedelta(days=UNKNOWN_TERMS_DAYS))


def test_round_off_is_other_charges_not_taxable_value():
    r = run()
    e = by_no(r, "INV-005", date(2025, 4, 12))
    assert (e["gross_paise"], e["gst_paise"]) == (1000040, 152549)


def test_rejects_name_their_cause_and_line():
    reasons = {(x.source_row, x.reason) for x in run().rejects}
    assert reasons == {(20, "total_mismatch"), (29, "bad_amount"), (32, "bad_date"), (35, "missing_field")}


def test_buyers_resolve_by_name_since_a_day_book_has_no_gstin():
    assert {x.method for x in run().resolutions} == {"name"}


@pytest.mark.parametrize("text,d", [("07-Apr-25", date(2025, 4, 7)), ("31-Mar-26", date(2026, 3, 31))])
def test_tally_dates(text, d):
    assert parse_tally_date(text) == d


@pytest.mark.parametrize("text", ["07/04/2025", "7-April-25", "2025-04-07"])
def test_other_date_styles_are_not_tally(text):
    with pytest.raises(FieldError):
        parse_tally_date(text)


def test_file_without_the_header_is_refused():
    with pytest.raises(FieldError, match="header"):
        TallyDayBookAdapter().parse(b"Date,Narration,Amount\n07-Apr-25,x,1\n")


def test_invoice_id_uses_the_bill_reference_not_the_voucher_number():
    r = run()
    assert ids.invoice_id(TENANT, SUPPLIER, "1", "2025-26") not in set(r.invoices["invoice_id"])
