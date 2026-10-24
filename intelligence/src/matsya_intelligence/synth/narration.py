"""Indian bank narration templates per rail. Formats are plausible, not bank-exact.

Every template comes in variants (0, 1). Variant 0 is hyphen-separated
("NEFT CR-..."), variant 1 slash-separated ("BY NEFT/..."), loosely like two
bank families. A Style restricts which variants and which bank codes a world
uses, so the categoriser can be trained on some and tested on held-out ones.
"""

from dataclasses import dataclass
from datetime import date

import numpy as np

ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BANKS = ("HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB", "BARB", "CNRB")
VPA_HANDLES = ["okhdfcbank", "okicici", "oksbi", "ybl", "paytm", "axl"]
CITIES = ["SURAT", "TIRUPUR", "LUDHIANA", "PUNE", "COIMBATORE", "RAJKOT"]
TREDS_PLATFORMS = ["RXIL TREDS", "M1XCHANGE", "INVOICEMART"]
LENDERS = ["BAJAJ FINANCE", "TATA CAPITAL", "HDFC BANK LOAN", "SIDBI"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
N_VARIANTS = 2

_NAME_A = ["SHREE", "NAV", "BHAGYA", "SURYA", "OM", "MAHA", "JAI", "SAI", "KAVERI", "ANAND"]
_NAME_B = ["GANESH", "SHAKTI", "LAXMI", "DURGA", "VIJAY", "KRISHNA", "AMBA", "UMIYA"]
_SECTOR_WORD = {
    "textiles": ["TEXFAB", "SYNTEX", "WEAVERS", "GARMENTS"],
    "auto_components": ["AUTOTECH", "FORGINGS", "AUTO PARTS", "CASTINGS"],
    "fmcg": ["FOODS", "CONSUMER", "AGRO", "TRADERS"],
}
_PERSON_FIRST = ["RAMESH", "SUNITA", "ARJUN", "PRIYA", "MOHAN", "KAVITA", "IMRAN", "DEEPA"]
_PERSON_LAST = ["PATEL", "SHARMA", "IYER", "REDDY", "KHAN", "SINGH", "NAIR", "JAIN"]
_SHOPS = ["STATIONERS", "HARDWARE", "MEDICALS", "TEA STALL", "XEROX CENTRE", "PETROL PUMP", "KIRANA STORE"]


@dataclass(frozen=True)
class Style:
    variants: tuple[int, ...] = (0, 1)
    banks: tuple[str, ...] = BANKS


def _v(rng: np.random.Generator, st: Style) -> int:
    return int(st.variants[int(rng.integers(len(st.variants)))])


def _bank(rng: np.random.Generator, st: Style) -> str:
    return st.banks[int(rng.integers(len(st.banks)))]


def digits(rng: np.random.Generator, n: int) -> str:
    return "".join(str(d) for d in rng.integers(0, 10, n))


def letters(rng: np.random.Generator, n: int) -> str:
    return "".join(chr(65 + c) for c in rng.integers(0, 26, n))


def company_name(rng: np.random.Generator, sector: str, btype: str) -> str:
    word = _SECTOR_WORD.get(sector, ["INDUSTRIES"])
    base = f"{rng.choice(_NAME_A)} {rng.choice(_NAME_B)} {rng.choice(word)}"
    if btype == "cpse":
        return f"{base} NIGAM LTD"
    return f"{base} {'LTD' if btype == 'listed' else 'PVT LTD'}"


def person_name(rng: np.random.Generator) -> str:
    return f"{rng.choice(_PERSON_FIRST)} {rng.choice(_PERSON_LAST)}"


def shop_name(rng: np.random.Generator) -> str:
    return f"{rng.choice(_NAME_B)} {rng.choice(_SHOPS)}"


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        prod = ALNUM.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return ALNUM[(36 - total % 36) % 36]


def make_pan(rng: np.random.Generator, holder: str = "C") -> str:
    return f"{letters(rng, 3)}{holder}{letters(rng, 1)}{digits(rng, 4)}{letters(rng, 1)}"


def make_gstin(rng: np.random.Generator, pan: str) -> str:
    first14 = f"{rng.integers(1, 38):02d}{pan}1Z"
    return first14 + gstin_checksum(first14)


def ifsc(rng: np.random.Generator, st: Style) -> str:
    return f"{_bank(rng, st)}0{digits(rng, 6)}"


def utr(rng: np.random.Generator, bank: str, d: date, rail_char: str) -> str:
    return f"{bank}{rail_char}{d:%y}{d.timetuple().tm_yday:03d}{digits(rng, 8)}"


def vpa(rng: np.random.Generator, name: str) -> str:
    handle = name.lower().replace(" ", "")[:10]
    return f"{handle}@{rng.choice(VPA_HANDLES)}"


def _upi(rng, st: Style, name: str, note: str, credit: bool) -> tuple[str, str]:
    code, rrn = ifsc(rng, st), digits(rng, 12)
    if _v(rng, st) == 0:
        return "UPI", f"UPI-{name}-{vpa(rng, name)}-{code}-{rrn}-{note}"
    return "UPI", f"UPI/{'CR' if credit else 'DR'}/{rrn}/{name}/{code[:4]}/{vpa(rng, name)}/{note}"


def _imps(rng, st: Style, name: str, note: str) -> tuple[str, str]:
    bank, rrn = _bank(rng, st), digits(rng, 12)
    if _v(rng, st) == 0:
        return "IMPS", f"IMPS-{rrn}-{name}-{bank}-XXXXXXXX{digits(rng, 4)}-{note}"
    return "IMPS", f"MMT/IMPS/{rrn}/{note}/{name}/{bank}"


def credit_from_company(rng, st: Style, name: str, d: date, ref: str, amount_paise: int) -> tuple[str, str]:
    """Large B2B credits go RTGS (>= Rs 2 lakh) or NEFT."""
    code = ifsc(rng, st)
    bank = code[:4]
    rtgs = amount_paise >= 2_00_000_00 and rng.random() < 0.6
    if _v(rng, st) == 0:
        if rtgs:
            return "RTGS", f"RTGS CR-{code}-{name}-{bank}R{d:%Y%m%d}{digits(rng, 8)}-{ref}"
        return "NEFT", f"NEFT CR-{code}-{name}-{ref}-{utr(rng, bank, d, 'N')}"
    if rtgs:
        return "RTGS", f"BY RTGS/{bank}R{d:%Y%m%d}{digits(rng, 8)}/{name}/{ref}"
    return "NEFT", f"BY NEFT/{utr(rng, bank, d, 'N')}/{name}/{ref}"


def credit_via_treds(rng, st: Style, d: date, ref: str) -> tuple[str, str]:
    code = ifsc(rng, st)
    platform = rng.choice(TREDS_PLATFORMS)
    if _v(rng, st) == 0:
        return "NEFT", f"NEFT CR-{code}-{platform} SETTLEMENT-{ref}-{utr(rng, code[:4], d, 'N')}"
    return "NEFT", f"BY NEFT/{utr(rng, code[:4], d, 'N')}/{platform}/{ref}"


def debit_to_party(rng, st: Style, name: str, d: date, note: str, amount_paise: int) -> tuple[str, str]:
    if amount_paise < 1_00_000_00 and rng.random() < 0.5:
        return _upi(rng, st, name, note, credit=False)
    if amount_paise < 5_00_000 and rng.random() < 0.5:
        return _imps(rng, st, name, note)
    code = ifsc(rng, st)
    if _v(rng, st) == 0:
        return "NEFT", f"NEFT DR-{code}-{name}-{note}-{utr(rng, code[:4], d, 'N')}"
    return "NEFT", f"TO NEFT/{utr(rng, code[:4], d, 'N')}/{name}/{note}"


def upi_credit(rng, st: Style, name: str, note: str) -> tuple[str, str]:
    return _upi(rng, st, name, note, credit=True)


def upi_debit(rng, st: Style, name: str, note: str) -> tuple[str, str]:
    return _upi(rng, st, name, note, credit=False)


def gst_payment(rng, st: Style, gstin: str) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "NEFT", f"IB BILLPAY DR-GSTN-CPIN {digits(rng, 14)}-{gstin}"
    return "NEFT", f"GST/CPIN/{digits(rng, 14)}/{gstin}/TAX PAYMENT"


def tds_payment(rng, st: Style, d: date) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "NEFT", f"TAX PMT CIN {digits(rng, 7)}{d:%d%m%y}{digits(rng, 5)} ITNS281"
    return "NEFT", f"OLTAS/{digits(rng, 7)}/CHALLAN 281/TDS/{d:%d%m%y}"


def pf_payment(rng, st: Style) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "NEFT", f"EPFO ECR TRRN {digits(rng, 13)}"
    return "NEFT", f"EPF CHALLAN/{digits(rng, 13)}/EPFO"


def esi_payment(rng, st: Style) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "NEFT", f"ESIC CHALLAN {digits(rng, 17)}"
    return "NEFT", f"ESI CONTRIBUTION/{digits(rng, 10)}/ESIC"


def salary(rng, st: Style, d: date) -> tuple[str, str]:
    mon = MONTHS[(d.month - 2) % 12]
    if _v(rng, st) == 0:
        return "NEFT", f"BULK UPLOAD SALARY {mon}{d:%y} {digits(rng, 6)}"
    return "NEFT", f"SAL/{mon}{d:%y}/BULK/{rng.integers(8, 40)} EMPLOYEES"


def ach_emi(rng, st: Style, lender: str) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "ACH", f"ACH D- {lender}-{letters(rng, 4)}{digits(rng, 16)}"
    return "ACH", f"NACH/DR/{letters(rng, 4)}{digits(rng, 16)}/{lender}/EMI"


def bank_charge(rng, st: Style, d: date, quarterly: bool) -> tuple[str, str]:
    v = _v(rng, st)
    if quarterly:
        q = MONTHS[(d.month - 4) % 12] + "-" + MONTHS[(d.month - 2) % 12]
        return "CHARGES", f"SMS CHGS {q} {d:%y}+GST" if v == 0 else f"CHRG/SMS ALERT/{q}{d:%y}"
    if v == 0:
        return "CHARGES", str(rng.choice(["NEFT CHGS INCL GST", "CONSOLIDATED CHGS FOR A/C", "AMC CHGS DEBIT CARD"]))
    return "CHARGES", str(rng.choice(["CHRG/NEFT/GST", "CHRG/ACCOUNT MAINTENANCE", "CHRG/DEBIT CARD ANNUAL FEE"]))


def cash_withdrawal(rng, st: Style) -> tuple[str, str]:
    if _v(rng, st) == 0:
        return "ATM", f"ATW-{digits(rng, 6)}XXXXXX{digits(rng, 4)}-S1A{digits(rng, 5)}-{rng.choice(CITIES)}"
    return "ATM", f"CASH WDL/ATM/{digits(rng, 8)}/{rng.choice(CITIES)}"


def owner_transfer(rng, st: Style, owner: str) -> tuple[str, str]:
    bank, rrn = _bank(rng, st), digits(rng, 12)
    if _v(rng, st) == 0:
        return "IMPS", f"IMPS-{rrn}-{owner}-{bank}-XXXXXXXX{digits(rng, 4)}-SELF"
    return "IMPS", f"MMT/IMPS/{rrn}/OWN ACCOUNT/{owner}/{bank}"
