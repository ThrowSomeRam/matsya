"""ledger.invoices -> a sloppy owner-style invoice CSV, as exported by hand from a small
business's books.

Format noise only, seeded: a UTF-8 BOM, CRLF line ends, header names with stray spaces, a
date format chosen per row (all day-first), rupee amounts with Indian comma grouping and
".00" sometimes dropped, buyer names in mixed case, and a trailing blank row. Facts come
from the issue-time version of each invoice. An invoice whose terms_source is unknown gets
no Credit Days and no Due Date, so the importer has a real reason to say unknown.
"""

import csv
import io
from datetime import date

import polars as pl

from render.faults import rng

HEADER = ["Invoice No", " Invoice Date ", "Customer Name", "Customer GSTIN", "Credit Days", "Due Date",
          "Taxable Value", "GST Amount", "Invoice Total"]
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d.%m.%Y", "%d/%m/%y")


def indian_rupees(paise: int, drop_zero_decimals: bool) -> str:
    rupees, p = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return s if (p == 0 and drop_zero_decimals) else f"{s}.{p:02d}"


def _name_case(name: str, r) -> str:
    return r.choice([name, name.title(), name.lower().capitalize()])


def issue_versions(invoices: pl.DataFrame) -> pl.DataFrame:
    """The first version of each invoice: what the owner's books show at issue."""
    return invoices.sort("known_at").group_by("tenant_id", "invoice_id", maintain_order=True).first()


def render(invoices: pl.DataFrame, invoice_no: dict[str, str], buyers: pl.DataFrame, seed: int,
           duplicate_rows: dict[str, str], missing_gstin: dict[str, str]) -> tuple[bytes, pl.DataFrame]:
    """(CSV bytes, row map: source_row -> invoice_id, fault_id, duplicate_of_row)."""
    r = rng(seed, "csv_generic")
    names = dict(zip(buyers["buyer_id"], buyers["name"]))
    gstins = dict(zip(buyers["buyer_id"], buyers["gstin"]))
    first = issue_versions(invoices).with_columns(
        pl.col("invoice_id").replace_strict(invoice_no, return_dtype=pl.String).alias("invoice_no")
    ).sort("issue_date", "invoice_no")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(HEADER)
    rows, n = [], 0
    for inv in first.iter_rows(named=True):
        fmt = r.choice(DATE_FORMATS)
        drop = r.random() < 0.5

        def d(x: date | None) -> str:
            return x.strftime(fmt) if x else ""

        unknown = inv["terms_source"] == "unknown"
        gst = inv["gst_paise"] or 0
        cells = [inv["invoice_no"], d(inv["issue_date"]), _name_case(names[inv["buyer_id"]], r),
                 "" if inv["invoice_id"] in missing_gstin else (gstins[inv["buyer_id"]] or ""),
                 "" if unknown else str(inv["terms_days"]), "" if unknown else d(inv["due_date"]),
                 indian_rupees(inv["gross_paise"] - gst, drop), indian_rupees(gst, drop),
                 indian_rupees(inv["gross_paise"], drop)]
        w.writerow(cells)
        n += 1
        own = n
        rows.append({"source_row": n, "invoice_id": inv["invoice_id"],
                     "fault_id": missing_gstin.get(inv["invoice_id"]), "duplicate_of_row": None})
        if inv["invoice_id"] in duplicate_rows:
            no = inv["invoice_no"]
            spaced = f" {no[:3]} {no[3:]} " if len(no) > 3 else f" {no} "
            w.writerow([spaced, cells[1], "  " + cells[2].replace(" ", "  ") + " ", *cells[3:]])
            n += 1
            rows.append({"source_row": n, "invoice_id": inv["invoice_id"],
                         "fault_id": duplicate_rows[inv["invoice_id"]], "duplicate_of_row": own})
    w.writerow([""] * len(HEADER))
    data = "﻿" + out.getvalue()
    rmap = pl.DataFrame(rows, schema={"source_row": pl.Int32, "invoice_id": pl.String, "fault_id": pl.String,
                                      "duplicate_of_row": pl.Int32})
    return data.encode("utf-8"), rmap
