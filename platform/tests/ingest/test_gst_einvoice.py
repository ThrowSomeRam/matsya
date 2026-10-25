"""The GST e-invoice adapter against hand-written documents. The shape is the schema as
understood, not a verified sample (see the adapter's docstring)."""

import json
from datetime import date

import pytest
from conftest import TENANT
from test_invoices_csv import ACME_PAN, RECEIVED, SUPPLIER, buyers, inv_id

from matsya_platform.ingest.fields import FieldError
from matsya_platform.ingest.gst_einvoice import GstEInvoiceAdapter
from matsya_platform.ingest.invoices import ingest

IRN = "a" * 63 + "F"


def doc(no="INV-001", dt="07/04/2025", crday=30, seller=SUPPLIER, typ="INV", gstin=f"27{ACME_PAN}1Z5",
        items=((100000.50, 9000.00, 9000.00, 0),), rnd=0, irn=IRN):
    il = [{"SlNo": str(k + 1), "AssAmt": a, "CgstAmt": c, "SgstAmt": s, "IgstAmt": i,
           "TotItemVal": round(a + c + s + i, 2)} for k, (a, c, s, i) in enumerate(items)]
    tot = lambda f: round(sum(x[f] for x in il), 2)  # noqa: E731
    d = {"Irn": irn, "Version": "1.1", "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B"},
         "DocDtls": {"Typ": typ, "No": no, "Dt": dt},
         "SellerDtls": {"Gstin": seller, "LglNm": "SHARMA FABRICS PVT LTD"},
         "BuyerDtls": {"Gstin": gstin, "LglNm": "ACME TEXTILES LTD"}, "ItemList": il,
         "ValDtls": {"AssVal": tot("AssAmt"), "CgstVal": tot("CgstAmt"), "SgstVal": tot("SgstAmt"),
                     "IgstVal": tot("IgstAmt"), "CesVal": 0, "RndOffAmt": rnd,
                     "TotInvVal": round(tot("TotItemVal") + rnd, 2)},
         "PayDtls": {"Nm": "SHARMA FABRICS PVT LTD"}}
    if crday is not None:
        d["PayDtls"]["CrDay"] = crday
    return d


def run(docs):
    return ingest(json.dumps(docs).encode(), GstEInvoiceAdapter(), tenant_id=TENANT, supplier_gstin=SUPPLIER,
                  received_at=RECEIVED, buyers=buyers())


def test_document_becomes_an_invoice_with_its_irn():
    r = run([doc()])
    (row,) = r.invoices.iter_rows(named=True)
    assert row["invoice_id"] == inv_id("INV-001", date(2025, 4, 7))
    assert (row["gross_paise"], row["gst_paise"], row["terms_days"], row["terms_source"]) == \
        (11800050, 1800000, 30, "document_terms")
    assert row["irn"] == IRN.lower() and r.resolutions[0].method == "gstin"


def test_single_document_file_is_accepted():
    assert run(doc()).invoices.height == 1


def test_absent_crday_means_unknown_terms():
    (row,) = run([doc(crday=None)]).invoices.iter_rows(named=True)
    assert (row["terms_days"], row["terms_source"]) == (None, "unknown")


def test_items_are_summed_and_round_off_is_not_taxable():
    (row,) = run([doc(items=((50000.25, 0, 0, 9000.05), (25000.10, 0, 0, 4500.02)), rnd=-0.42)]).invoices \
        .iter_rows(named=True)
    assert (row["gross_paise"], row["gst_paise"]) == (8850000, 1350007)


def test_rejects():
    bad_items = doc(no="INV-5")
    bad_items["ValDtls"]["AssVal"] = 1.00
    three_dp = doc(no="INV-6")
    three_dp["ValDtls"]["CesVal"] = 0.001
    r = run([doc(no="INV-2", typ="CRN"), doc(no="INV-3", seller="29AAFCZ9999K1Z2"), doc(no="INV-4", dt="2025-04-07"),
             bad_items, three_dp, doc(no="INV-7", irn="xyz"), "not an object", doc(no="INV-8", gstin="URP")])
    assert sorted((x.source_row, x.reason) for x in r.rejects) == [
        (1, "unsupported_document"), (2, "wrong_supplier"), (3, "bad_date"), (4, "total_mismatch"),
        (5, "bad_amount"), (6, "malformed"), (7, "malformed")]
    assert r.resolutions[0].method == "name"  # URP: no GSTIN, resolved by the buyer's name


def test_not_json_is_refused():
    with pytest.raises(FieldError, match="not JSON"):
        GstEInvoiceAdapter().parse(b"<Invoice/>")
