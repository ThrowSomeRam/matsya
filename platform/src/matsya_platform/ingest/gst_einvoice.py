"""GST e-invoice JSON adapter. NOT verified against a real e-invoice: no verified sample was
available, and nothing in this repo or in Track B's generator pins the shape (the generator's
irn is a sha256 placeholder, "valid in shape, not a real IRN"). The field names below follow
the e-invoice schema as understood, and the round trip in eval/platform only proves that our
renderer and this reader agree with each other.

Assumed shape: a file is one JSON document or a list of them. Each document is the decoded
signed invoice:
- Irn (64 hex characters), DocDtls {Typ, No, Dt as DD/MM/YYYY};
- SellerDtls {Gstin, LglNm}, BuyerDtls {Gstin, LglNm, TrdNm};
- ItemList [{AssAmt, CgstAmt, SgstAmt, IgstAmt, CesAmt, TotItemVal}];
- ValDtls {AssVal, CgstVal, SgstVal, IgstVal, CesVal, RndOffAmt, OthChrg, TotInvVal};
- PayDtls {CrDay}, absent when terms are unknown.

Amounts are JSON numbers in rupees. They are read as Decimal, never float, and must have at
most two decimals. ValDtls must agree with the sum of ItemList. Only Typ INV is an invoice:
CRN and DBN (credit and debit notes) are rejected as unsupported_document until notes have
their own path. Buyer GSTIN "URP" (unregistered person) counts as no GSTIN.
"""

import json
from datetime import datetime
from decimal import Decimal

from matsya_platform.ingest.fields import FieldError
from matsya_platform.ingest.invoices import InvoiceLine, Reject

_TAX = (("CgstAmt", "CgstVal"), ("SgstAmt", "SgstVal"), ("IgstAmt", "IgstVal"), ("CesAmt", "CesVal"))


def _paise(v, field: str) -> int:
    if v is None:
        return 0
    if not isinstance(v, (Decimal, int)) or isinstance(v, bool):
        raise FieldError(f"bad amount {field}={v!r}")
    d = Decimal(v) * 100
    if d != d.to_integral_value():
        raise FieldError(f"bad amount {field}={v!r}: more than two decimals")
    return int(d)


def _get(doc: dict, *path: str):
    cur = doc
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            raise FieldError(f"missing field {'.'.join(path)}")
        cur = cur[p]
    return cur


class GstEInvoiceAdapter:
    name = "gst_einvoice"

    def parse(self, data: bytes) -> tuple[list[InvoiceLine], list[Reject]]:
        try:
            parsed = json.loads(data.decode("utf-8-sig"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise FieldError(f"e-invoice file is not JSON: {e}") from None
        docs = parsed if isinstance(parsed, list) else [parsed]
        lines, rejects = [], []
        for n, doc in enumerate(docs, start=1):
            try:
                if not isinstance(doc, dict):
                    raise FieldError("malformed: document is not an object")
                lines.append(self._doc(n, doc))
            except FieldError as e:
                msg = str(e)
                reason = ("malformed" if msg.startswith("malformed") else "unsupported_document"
                          if msg.startswith("unsupported") else "bad_date" if "date" in msg
                          else "total_mismatch" if "disagree" in msg else "bad_amount" if "amount" in msg
                          else "missing_field")
                rejects.append(Reject(n, reason, msg))
        return lines, rejects

    def _doc(self, n: int, doc: dict) -> InvoiceLine:
        typ = _get(doc, "DocDtls", "Typ")
        if typ != "INV":
            raise FieldError(f"unsupported document type {typ!r}")
        try:
            issue = datetime.strptime(str(_get(doc, "DocDtls", "Dt")), "%d/%m/%Y").date()
        except ValueError:
            raise FieldError(f"bad date DocDtls.Dt={doc['DocDtls']['Dt']!r}") from None
        val = _get(doc, "ValDtls")
        items = _get(doc, "ItemList")
        if not isinstance(items, list) or not items:
            raise FieldError("missing field ItemList")
        taxable = _paise(_get(val, "AssVal"), "AssVal")
        gst = sum(_paise(val.get(v), v) for _, v in _TAX)
        gross = _paise(_get(val, "TotInvVal"), "TotInvVal")
        other = _paise(val.get("RndOffAmt"), "RndOffAmt") + _paise(val.get("OthChrg"), "OthChrg")
        if sum(_paise(i.get("AssAmt"), "AssAmt") for i in items) != taxable or \
                sum(_paise(i.get(a), a) for i in items for a, _ in _TAX) != gst:
            raise FieldError("ItemList and ValDtls disagree")
        buyer = _get(doc, "BuyerDtls")
        bgstin = buyer.get("Gstin")
        pay = doc.get("PayDtls") or {}
        crday = pay.get("CrDay")
        if crday is not None and (not isinstance(crday, int) or isinstance(crday, bool) or crday < 0):
            raise FieldError(f"bad amount PayDtls.CrDay={crday!r}")
        irn = doc.get("Irn")
        if irn is not None and not (isinstance(irn, str) and len(irn) == 64
                                    and all(c in "0123456789abcdef" for c in irn.lower())):
            raise FieldError(f"malformed Irn {irn!r}")
        return InvoiceLine(
            source_row=n, invoice_no=str(_get(doc, "DocDtls", "No")), issue_date=issue,
            buyer_name=buyer.get("LglNm") or buyer.get("TrdNm"),
            buyer_gstin=None if bgstin in (None, "", "URP") else bgstin,
            gross_paise=gross, gst_paise=gst, taxable_paise=taxable, other_paise=other,
            credit_days=crday, due_date=None, irn=irn.lower() if irn else None,
            seller_gstin=str(_get(doc, "SellerDtls", "Gstin")))
