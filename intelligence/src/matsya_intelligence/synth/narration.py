"""Indian bank narration templates per rail. Formats are plausible, not bank-exact."""

from datetime import date

import numpy as np

ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB", "BARB", "CNRB"]
VPA_HANDLES = ["okhdfcbank", "okicici", "oksbi", "ybl", "paytm", "axl"]
CITIES = ["SURAT", "TIRUPUR", "LUDHIANA", "PUNE", "COIMBATORE", "RAJKOT"]
TREDS_PLATFORMS = ["RXIL TREDS", "M1XCHANGE", "INVOICEMART"]
LENDERS = ["BAJAJ FINANCE", "TATA CAPITAL", "HDFC BANK LOAN", "SIDBI"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

_NAME_A = ["SHREE", "NAV", "BHAGYA", "SURYA", "OM", "MAHA", "JAI", "SAI", "KAVERI", "ANAND"]
_NAME_B = ["GANESH", "SHAKTI", "LAXMI", "DURGA", "VIJAY", "KRISHNA", "AMBA", "UMIYA"]
_SECTOR_WORD = {
    "textiles": ["TEXFAB", "SYNTEX", "WEAVERS", "GARMENTS"],
    "auto_components": ["AUTOTECH", "FORGINGS", "AUTO PARTS", "CASTINGS"],
    "fmcg": ["FOODS", "CONSUMER", "AGRO", "TRADERS"],
}
_PERSON_FIRST = ["RAMESH", "SUNITA", "ARJUN", "PRIYA", "MOHAN", "KAVITA", "IMRAN", "DEEPA"]
_PERSON_LAST = ["PATEL", "SHARMA", "IYER", "REDDY", "KHAN", "SINGH", "NAIR", "JAIN"]


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


def ifsc(rng: np.random.Generator) -> str:
    return f"{rng.choice(BANKS)}0{digits(rng, 6)}"


def utr(rng: np.random.Generator, bank: str, d: date, rail_char: str) -> str:
    return f"{bank}{rail_char}{d:%y}{d.timetuple().tm_yday:03d}{digits(rng, 8)}"


def vpa(rng: np.random.Generator, name: str) -> str:
    handle = name.lower().replace(" ", "")[:10]
    return f"{handle}@{rng.choice(VPA_HANDLES)}"


def credit_from_company(rng, name: str, d: date, ref: str, amount_paise: int) -> tuple[str, str]:
    """Large B2B credits go RTGS (>= Rs 2 lakh) or NEFT."""
    code = ifsc(rng)
    bank = code[:4]
    if amount_paise >= 2_00_000_00 and rng.random() < 0.6:
        return "RTGS", f"RTGS CR-{code}-{name}-{bank}R{d:%Y%m%d}{digits(rng, 8)}-{ref}"
    return "NEFT", f"NEFT CR-{code}-{name}-{ref}-{utr(rng, bank, d, 'N')}"


def credit_via_treds(rng, d: date, ref: str) -> tuple[str, str]:
    code = ifsc(rng)
    return "NEFT", f"NEFT CR-{code}-{rng.choice(TREDS_PLATFORMS)} SETTLEMENT-{ref}-{utr(rng, code[:4], d, 'N')}"


def debit_to_party(rng, name: str, d: date, note: str, amount_paise: int) -> tuple[str, str]:
    code = ifsc(rng)
    if amount_paise < 1_00_000_00 and rng.random() < 0.5:
        return "UPI", f"UPI-{name}-{vpa(rng, name)}-{code}-{digits(rng, 12)}-{note}"
    if amount_paise < 5_00_000 and rng.random() < 0.5:
        return "IMPS", f"IMPS-{digits(rng, 12)}-{name}-{code[:4]}-XXXXXXXX{digits(rng, 4)}-{note}"
    return "NEFT", f"NEFT DR-{code}-{name}-{note}-{utr(rng, code[:4], d, 'N')}"


def upi_credit(rng, name: str, note: str) -> tuple[str, str]:
    return "UPI", f"UPI-{name}-{vpa(rng, name)}-{ifsc(rng)}-{digits(rng, 12)}-{note}"


def gst_payment(rng, gstin: str) -> tuple[str, str]:
    return "NEFT", f"IB BILLPAY DR-GSTN-CPIN {digits(rng, 14)}-{gstin}"


def tds_payment(rng, d: date) -> tuple[str, str]:
    return "NEFT", f"TAX PMT CIN {digits(rng, 7)}{d:%d%m%y}{digits(rng, 5)} ITNS281"


def pf_payment(rng) -> tuple[str, str]:
    return "NEFT", f"EPFO ECR TRRN {digits(rng, 13)}"


def esi_payment(rng) -> tuple[str, str]:
    return "NEFT", f"ESIC CHALLAN {digits(rng, 17)}"


def salary(rng, d: date) -> tuple[str, str]:
    mon = MONTHS[(d.month - 2) % 12]
    return "NEFT", f"BULK UPLOAD SALARY {mon}{d:%y} {digits(rng, 6)}"


def ach_emi(rng, lender: str) -> tuple[str, str]:
    return "ACH", f"ACH D- {lender}-{letters(rng, 4)}{digits(rng, 16)}"


def bank_charge(rng, d: date, quarterly: bool) -> tuple[str, str]:
    if quarterly:
        q = MONTHS[(d.month - 4) % 12] + "-" + MONTHS[(d.month - 2) % 12]
        return "CHARGES", f"SMS CHGS {q} {d:%y}+GST"
    return "CHARGES", str(rng.choice(["NEFT CHGS INCL GST", "CONSOLIDATED CHGS FOR A/C", "AMC CHGS DEBIT CARD"]))


def cash_withdrawal(rng) -> tuple[str, str]:
    return "ATM", f"ATW-{digits(rng, 6)}XXXXXX{digits(rng, 4)}-S1A{digits(rng, 5)}-{rng.choice(CITIES)}"


def owner_transfer(rng, owner: str) -> tuple[str, str]:
    return "IMPS", f"IMPS-{digits(rng, 12)}-{owner}-{rng.choice(BANKS)}-XXXXXXXX{digits(rng, 4)}-SELF"
