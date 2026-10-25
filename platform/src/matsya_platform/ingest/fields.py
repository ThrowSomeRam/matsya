"""Field parsing shared by invoice adapters: rupee text to exact paise, day-first dates.

Amounts never pass through a float: text is checked against Indian, Western or no digit
grouping and converted with Decimal.
"""

import re
from datetime import date, datetime
from decimal import Decimal

DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y")
_GROUPED = re.compile(r"^\d{1,3}(,\d{2})*,\d{3}$|^\d{1,3}(,\d{3})+$|^\d+$")  # Indian, Western, none
_CURRENCY = re.compile(r"^(RS\.?|INR|₹)\s*", re.IGNORECASE)


class FieldError(ValueError):
    pass


def parse_rupees(text: str) -> int:
    s = _CURRENCY.sub("", text.strip()).strip()
    whole, _, frac = s.partition(".")
    if not whole or not _GROUPED.match(whole) or (frac and not re.fullmatch(r"\d{1,2}", frac)) or s.endswith("."):
        raise FieldError(f"bad amount {text!r}")
    return int(Decimal(whole.replace(",", "") + "." + (frac or "0")) * 100)


def parse_date(text: str) -> date:
    s = re.sub(r"\s+", " ", text.strip())
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if d.year >= 1990:  # %Y accepts two digits; let the %y formats handle those
            return d
    raise FieldError(f"bad date {text!r}")
