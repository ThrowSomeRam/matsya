"""ID formats. All IDs are opaque: compare them, never parse them.

- tenant_id: UUIDv4, lowercase canonical form.
- txn_uid: sha256(tenant | account | k), first 32 hex chars. k, in order of preference:
    txn:<bank txnId>|<value_date>|<signed amount_paise>
    utr:<UTR or RRN>|<signed amount_paise>
    bal:<post-transaction running balance>|<value_date>|<signed amount_paise>
    ord:<value_date>|<signed amount_paise>|<normalised narration>|<ordinal among identical rows>
  Banks reuse txnIds, so a bare txnId would collapse two real transactions into one row and
  lose money from the ledger; the same txnId arriving with different fields is reported as a
  txn_id_conflict event. One UTR can cover a payment and its bank charge, hence the amount.
  A running balance repeats across dates, hence the date and amount. Two identical same-day
  UPI credits must not collapse into one row, and an ordinal alone shifts when a later fetch
  covers a different window, so it is the last resort. Choosing the path is A4's job.
- invoice_id: sha256(tenant | supplier GSTIN | normalised invoice no | FY), first 32 hex
  chars. Never depends on IRN.
- buyer_id: sha256(PAN), first 32 hex chars, when the PAN is known (id_basis pan). With no
  PAN: sha256("noPAN:" + tenant_id + "|" + normalised name or VPA), first 32 hex chars
  (id_basis local). Local buyers are tenant-scoped and never pool across tenants.
"""

import hashlib
import re
from datetime import date
from typing import Annotated

from pydantic import StringConstraints

UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

TenantId = Annotated[str, StringConstraints(pattern=UUID4_PATTERN)]
Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
TxnUid = Hex32
InvoiceId = Hex32
BuyerId = Hex32
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Pan = Annotated[str, StringConstraints(pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")]
# Shape only; the checksum is checked by the narration parser (A5).
Gstin = Annotated[str, StringConstraints(pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")]
Ifsc = Annotated[str, StringConstraints(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")]
FiscalYear = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}$")]  # "2025-26"
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


def hash32(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def txn_uid(tenant_id: str, account_id: str, key: str) -> str:
    """key is one of the txn_key_* results below."""
    return hash32(tenant_id, account_id, key)


def txn_key_bank_id(bank_txn_id: str, value_date: date, amount_paise: int) -> str:
    return f"txn:{bank_txn_id}|{value_date.isoformat()}|{amount_paise}"


def txn_key_utr(utr: str, amount_paise: int) -> str:
    return f"utr:{utr}|{amount_paise}"


def txn_key_balance(balance_after_paise: int, value_date: date, amount_paise: int) -> str:
    return f"bal:{balance_after_paise}|{value_date.isoformat()}|{amount_paise}"


def txn_key_ordinal(value_date: date, amount_paise: int, narration: str, ordinal: int) -> str:
    return f"ord:{value_date.isoformat()}|{amount_paise}|{normalise_party(narration)}|{ordinal}"


def normalise_party(text: str) -> str:
    """Collapse whitespace and uppercase: for buyer names, VPAs and narrations."""
    return " ".join(text.split()).upper()


def normalise_invoice_no(invoice_no: str) -> str:
    return re.sub(r"\s+", "", invoice_no).upper()


def invoice_id(tenant_id: str, supplier_gstin: str, invoice_no: str, fiscal_year: str) -> str:
    return hash32(tenant_id, supplier_gstin, normalise_invoice_no(invoice_no), fiscal_year)


def buyer_id(pan: str) -> str:
    return hash32(pan)


def local_buyer_id(tenant_id: str, name_or_vpa: str) -> str:
    return hash32("noPAN:" + tenant_id, normalise_party(name_or_vpa))


def pan_of_gstin(gstin: str) -> str:
    """Characters 3 to 12 (1-indexed) of a GSTIN are the holder's PAN."""
    # VERIFY: GSTIN structure, chars 3 to 12 = PAN
    return gstin[2:12]
