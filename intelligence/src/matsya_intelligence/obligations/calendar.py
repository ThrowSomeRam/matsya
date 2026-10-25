"""Statutory due dates from the constants file (statutory_calendar).

TDS deposit: 7th of the next month; March deductions are due 30 April.
PF and ESI: 15th (hard deadlines).
GSTR-3B: monthly on the 20th, or QRMP on the 22nd / 24th (state group 1 / 2)
of the month after each quarter. GST dates on a Sunday move to the Monday.
Only GST is shifted; the constants file records the Sunday rule for GST only.
"""

from datetime import date, timedelta

from matsya_intelligence.statutory import qrmp_day
from matsya_intelligence.synth.world import load_constants



def _months(start: date, end: date):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _off_sunday(d: date) -> date:
    return d + timedelta(days=1) if d.weekday() == 6 else d


def statutory_dates(start: date, end: date, qrmp: bool = False, state_group: int = 1) -> list[tuple[str, date]]:
    """(kind, due_date) for kinds tds, pf, esi, gst with start <= due_date <= end."""
    cal = load_constants()["statutory_calendar"]["monthly"]
    out = []
    for y, m in _months(start - timedelta(days=31), end + timedelta(days=31)):
        prev_m = 12 if m == 1 else m - 1
        out.append(("tds", date(y, 4, 30) if prev_m == 3 else date(y, m, cal["tds_deposit"]["day"])))
        out.append(("pf", date(y, m, cal["pf_deposit"]["day"])))
        out.append(("esi", date(y, m, cal["esi_deposit"]["day"])))
        if not qrmp:
            out.append(("gst", _off_sunday(date(y, m, cal["gstr3b_monthly"]["day"]))))
        elif m in (1, 4, 7, 10):  # month after each quarter
            out.append(("gst", _off_sunday(date(y, m, qrmp_day(state_group)))))
    return sorted({(k, d) for k, d in out if start <= d <= end}, key=lambda x: (x[1], x[0]))
