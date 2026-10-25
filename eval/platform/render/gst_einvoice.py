"""ledger.invoices -> GST e-invoice JSON documents, as the schema is understood. NOT checked
against a verified e-invoice: no sample was available, and nothing in this repo or in Track
B's generator pins the shape. Reading it back only proves that this renderer and platform's
GstEInvoiceAdapter agree with each other.

Low volume on purpose: micro suppliers below the e-invoicing turnover threshold do not have
e-invoices, so cli.py sends only a small share of invoices this way, and only for buyers with
a GSTIN (a B2B e-invoice names the buyer's GSTIN).

One document per invoice in a JSON list: DocDtls (INV, number, DD/MM/YYYY), SellerDtls,
BuyerDtls, an ItemList of one to three lines whose amounts sum to the invoice, ValDtls
totals, and PayDtls.CrDay only when terms are known. Amounts are JSON numbers in rupees.
Irn is sha256(supplier GSTIN | FY | INV | invoice no), a stand-in of the right shape (64
hex), not an IRN from the registration portal.
"""

import hashlib
import json

import polars as pl

from render.csv_generic import issue_versions
from render.faults import rng


def fiscal_year(d) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def irn(supplier_gstin: str, invoice_no: str, issue) -> str:
    return hashlib.sha256(f"{supplier_gstin}|{fiscal_year(issue)}|INV|{invoice_no}".encode()).hexdigest()


def _rs(paise: int) -> float:
    return round(paise / 100, 2)  # float repr of a two-decimal value prints those two decimals


def _split(total: int, parts: list[int], whole: int) -> list[int]:
    """total shared in proportion to parts (which sum to whole), remainder on the last."""
    out = [total * p // whole if whole else 0 for p in parts[:-1]]
    return out + [total - sum(out)]


def render(invoices: pl.DataFrame, invoice_no: dict[str, str], buyers: pl.DataFrame, seed: int,
           duplicate_rows: dict[str, str], supplier_gstin: str,
           supplier_name: str = "SUPPLIER") -> tuple[bytes, pl.DataFrame]:
    """(JSON bytes, row map: source_row (document index) -> invoice_id, fault_id, duplicate_of_row, irn)."""
    r = rng(seed, "gst_einvoice")
    names = dict(zip(buyers["buyer_id"], buyers["name"]))
    gstins = dict(zip(buyers["buyer_id"], buyers["gstin"]))
    first = issue_versions(invoices).with_columns(
        pl.col("invoice_id").replace_strict(invoice_no, return_dtype=pl.String).alias("invoice_no")
    ).sort("issue_date", "invoice_no")
    docs, rmap = [], []

    def doc(inv, no):
        gross, gst = inv["gross_paise"], inv["gst_paise"] or 0
        taxable = gross - gst
        n = r.choice([1, 1, 2, 3]) if taxable >= 300 else 1
        cuts = sorted(r.sample(range(1, taxable), n - 1)) if n > 1 else []
        ass = [b - a for a, b in zip([0, *cuts], [*cuts, taxable])]
        tax = _split(gst, ass, taxable)
        buyer_gstin = gstins[inv["buyer_id"]]
        intra = buyer_gstin[:2] == supplier_gstin[:2]
        items, tot = [], {"c": 0, "s": 0, "i": 0}
        for k, (a, t) in enumerate(zip(ass, tax), start=1):
            c, s, i = (t // 2, t - t // 2, 0) if intra else (0, 0, t)
            tot["c"] += c
            tot["s"] += s
            tot["i"] += i
            items.append({"SlNo": str(k), "IsServc": "N", "HsnCd": "5208", "Qty": 1, "Unit": "NOS",
                          "UnitPrice": _rs(a), "TotAmt": _rs(a), "AssAmt": _rs(a),
                          "GstRt": round(100 * gst / taxable) if taxable else 0, "CgstAmt": _rs(c),
                          "SgstAmt": _rs(s), "IgstAmt": _rs(i), "CesAmt": 0, "TotItemVal": _rs(a + t)})
        pay = {"Nm": supplier_name}
        if inv["terms_source"] != "unknown":
            pay["CrDay"] = int(inv["terms_days"])
        code = irn(supplier_gstin, inv["invoice_no"], inv["issue_date"])
        docs.append({
            "Irn": code, "AckNo": 100000000000 + len(docs), "Version": "1.1",
            "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B", "RegRev": "N"},
            "DocDtls": {"Typ": "INV", "No": no, "Dt": inv["issue_date"].strftime("%d/%m/%Y")},
            "SellerDtls": {"Gstin": supplier_gstin, "LglNm": supplier_name, "Stcd": supplier_gstin[:2]},
            "BuyerDtls": {"Gstin": buyer_gstin, "LglNm": names[inv["buyer_id"]], "Pos": buyer_gstin[:2],
                          "Stcd": buyer_gstin[:2]},
            "ItemList": items,
            "ValDtls": {"AssVal": _rs(taxable), "CgstVal": _rs(tot["c"]), "SgstVal": _rs(tot["s"]),
                        "IgstVal": _rs(tot["i"]), "CesVal": 0, "RndOffAmt": 0, "TotInvVal": _rs(gross)},
            "PayDtls": pay})
        return len(docs), code

    for inv in first.iter_rows(named=True):
        own, code = doc(inv, inv["invoice_no"])
        rmap.append({"source_row": own, "invoice_id": inv["invoice_id"], "fault_id": None, "duplicate_of_row": None,
                     "irn": code})
        if inv["invoice_id"] in duplicate_rows:
            no = inv["invoice_no"]
            n, _ = doc(inv, f" {no} ")
            rmap.append({"source_row": n, "invoice_id": inv["invoice_id"],
                         "fault_id": duplicate_rows[inv["invoice_id"]], "duplicate_of_row": own, "irn": code})
    return (json.dumps(docs, indent=1) + "\n").encode("utf-8"), pl.DataFrame(
        rmap, schema={"source_row": pl.Int32, "invoice_id": pl.String, "fault_id": pl.String,
                      "duplicate_of_row": pl.Int32, "irn": pl.String})
