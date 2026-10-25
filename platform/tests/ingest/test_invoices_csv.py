from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest
from conftest import FIXTURES, TENANT

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame, validate
from matsya_platform.ingest.invoices import UNKNOWN_TERMS_DAYS, BuyerDirectory, fiscal_year, ingest
from matsya_platform.ingest.invoices_csv import FieldError, GenericCsvAdapter, parse_date, parse_rupees

SUPPLIER = "29AAFCS1234K1Z2"
RECEIVED = datetime(2026, 4, 10, 4, 30, tzinfo=timezone.utc)
ACME_PAN, BETA_PAN = "ABCCA1234F", "BBCCB5678G"


def buyers() -> BuyerDirectory:
    rows = [T.Buyer(buyer_id=ids.buyer_id(ACME_PAN), id_basis="pan", pan=ACME_PAN, gstin=f"27{ACME_PAN}1Z5",
                    name="ACME TEXTILES LTD", type="listed", sector="textiles", sector_source="exchange",
                    treds_mandated=None),
            T.Buyer(buyer_id=ids.buyer_id(BETA_PAN), id_basis="pan", pan=BETA_PAN, gstin=None, name="BETA FOODS PVT LTD",
                    type="unlisted", sector="fmcg", sector_source="cin_nic", treds_mandated=None)]
    return BuyerDirectory(frame(T.Buyer, rows), None, TENANT)


def run(existing=frozenset()):
    data = (FIXTURES / "invoices_sloppy.csv").read_bytes()
    return ingest(data, GenericCsvAdapter(), tenant_id=TENANT, supplier_gstin=SUPPLIER, received_at=RECEIVED,
                  buyers=buyers(), existing_invoice_ids=existing)


def inv_id(no: str, d: date) -> str:
    return ids.invoice_id(TENANT, SUPPLIER, no, fiscal_year(d))


@pytest.mark.parametrize("text,paise", [("1,18,000.00", 11800000), ("1,180.59", 118059), ("59,000", 5900000),
                                        ("1,000.5", 100050), ("Rs. 1,23,456.78", 12345678), ("₹ 99", 9900),
                                        ("123,456.00", 12345600), ("0", 0)])
def test_rupees_to_paise(text, paise):
    assert parse_rupees(text) == paise


@pytest.mark.parametrize("text", ["1,00,00.00", "12,34", "1.234", "-500.00", "abc", "1,000."])
def test_bad_rupees(text):
    with pytest.raises(FieldError):
        parse_rupees(text)


@pytest.mark.parametrize("text,d", [("01/04/2025", date(2025, 4, 1)), ("01-05-2025", date(2025, 5, 1)),
                                    ("2025-04-03", date(2025, 4, 3)), ("05-Apr-2025", date(2025, 4, 5)),
                                    ("20.04.2025", date(2025, 4, 20)), ("01/04/25", date(2025, 4, 1))])
def test_dates_are_day_first(text, d):
    assert parse_date(text) == d


def test_fiscal_year():
    assert fiscal_year(date(2025, 4, 1)) == "2025-26" and fiscal_year(date(2026, 3, 31)) == "2025-26"


def test_ingest_sloppy_csv():
    r = run()
    validate(r.invoices, T.Invoice)
    got = {row["invoice_id"]: row for row in r.invoices.iter_rows(named=True)}
    assert set(got) == {inv_id("INV/001", date(2025, 4, 1)), inv_id("INV/002", date(2025, 4, 3)),
                        inv_id("INV/003", date(2025, 4, 5))}
    one = got[inv_id("INV/001", date(2025, 4, 1))]
    assert (one["gross_paise"], one["gst_paise"], one["terms_days"], one["due_date"], one["terms_source"]) == \
        (11800000, 1800000, 30, date(2025, 5, 1), "document_terms")
    assert one["status"] == "open" and one["irn"] is None and one["expected_tds_paise"] is None
    assert one["known_at"] == RECEIVED


def test_unknown_terms():
    two = run().invoices.filter(pl.col("invoice_id") == inv_id("INV/002", date(2025, 4, 3))).row(0, named=True)
    assert two["terms_source"] == "unknown" and two["terms_days"] is None
    assert two["due_date"] == date(2025, 4, 3) + timedelta(days=UNKNOWN_TERMS_DAYS)


def test_missing_gstin_resolves_by_name():
    r = run()
    methods = {x.source_row: x.method for x in r.resolutions}
    assert methods[1] == "gstin" and methods[3] == "name"
    three = r.invoices.filter(pl.col("invoice_id") == inv_id("INV/003", date(2025, 4, 5))).row(0, named=True)
    assert three["buyer_id"] == ids.buyer_id(BETA_PAN)


def test_whitespace_duplicate_is_dropped():
    (dup,) = run().duplicates
    assert (dup.source_row, dup.first_row, dup.identical) == (4, 1, True)


def test_rejects_are_reported_not_written():
    by_row = {x.source_row: x.reason for x in run().rejects}
    assert by_row == {5: "unresolved_buyer", 6: "bad_amount", 7: "total_mismatch", 8: "bad_date"}


def test_already_ingested_is_skipped():
    first = run()
    again = run(existing=frozenset(first.invoices["invoice_id"]))
    assert again.invoices.height == 0 and all(d.first_row is None or d.identical for d in again.duplicates)
