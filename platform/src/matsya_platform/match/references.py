"""Stage 1 parsing: invoice references in a narration.

ledger.invoices stores only invoice_id = hash(tenant, supplier GSTIN, invoice no, FY), not
the number itself (positions doc F8). A parsed reference is resolved by hashing its common
spellings (INV-00012, INV00012, INV/00012) for the fiscal years an invoice paid on this date
could belong to, and keeping the ids that exist.
"""

import re
from datetime import date, timedelta

from matsya_contracts import ids
from matsya_platform.ingest.invoices import fiscal_year

_REF = re.compile(r"\b(INV|INVOICE|BILL|TAX|SI|IN)[\s/\-#.]*(?:NO[\s.]*)?([A-Z]{0,3}\d{1,8})\b")


def spellings(narration: str | None) -> list[str]:
    text = ids.normalise_party(narration or "")
    out = []
    for prefix, num in _REF.findall(text):
        for sep in ("-", "", "/"):
            s = f"{prefix}{sep}{num}"
            if s not in out:
                out.append(s)
    return out


def resolve(narration: str | None, value_date: date, tenant_id: str, supplier_gstin: str, window_days: int,
            known: set[str]) -> list[str]:
    """Invoice ids the narration's references point to, among `known` ids."""
    years = sorted({fiscal_year(value_date), fiscal_year(value_date - timedelta(days=window_days))})
    hits = []
    for s in spellings(narration):
        for fy in years:
            i = ids.invoice_id(tenant_id, supplier_gstin, s, fy)
            if i in known and i not in hits:
                hits.append(i)
    return hits
