"""Recurrence detection on categorised outflows.

Group outflows by (category, counterparty). For each group with >= 3 payments:
  - gaps between consecutive dates;
  - period: the candidate P in {7, 14, 30, 90} days minimising median |gap - P| / P
    (30 stands for monthly; months are projected by calendar, not 30-day steps);
  - spread: MAD of the gaps; rejected if MAD > 0.25 P;
  - amount: median of the last 6 payments;
  - confidence: (1 - MAD / (0.25 P)) * min(1, n / 6), so tight, long series score high.
Next dates: monthly on the median day of month (clipped to month length), else
last date + k P.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.categorise.lfs import counterparty

PERIODS = (7, 14, 30, 90)
MAX_REL_MAD = 0.25
_MONTH_TOKEN = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\w*\b")


def stream_key(narration: str) -> str | None:
    """Counterparty with month names removed, so "SALARY MAR" and "SALARY APR" are one stream."""
    cp = counterparty(narration.upper())
    if cp is None:
        return None
    cp = " ".join(_MONTH_TOKEN.sub(" ", cp).split())
    return cp or None


@dataclass(frozen=True)
class Recurrence:
    category: str
    counterparty: str | None
    period_days: int
    day_of_month: int | None
    last_date: date
    amount_paise: int  # positive outflow
    confidence: float
    n: int

    def next_dates(self, after: date, until: date) -> list[date]:
        out = []
        if self.period_days == 30:
            y, m = after.year, after.month
            while True:
                d = date(y, m, min(self.day_of_month, calendar.monthrange(y, m)[1]))
                if d > until:
                    return out
                if d > after:
                    out.append(d)
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        d = self.last_date
        while (d := d + timedelta(days=self.period_days)) <= until:
            if d > after:
                out.append(d)
        return out


def _fit_group(category: str, cp: str | None, dates: list[date], amounts: list[int]) -> Recurrence | None:
    if len(dates) < 3:
        return None
    order = np.argsort(dates, kind="stable")
    dates = [dates[i] for i in order]
    amounts = [amounts[i] for i in order]
    gaps = np.diff([d.toordinal() for d in dates]).astype(float)
    if (gaps == 0).any():  # two payments on one day: not a single recurring stream
        return None
    period = min(PERIODS, key=lambda p: np.median(np.abs(gaps - p)) / p)
    mad = float(np.median(np.abs(gaps - np.median(gaps))))
    if mad > MAX_REL_MAD * period or abs(np.median(gaps) - period) > MAX_REL_MAD * period:
        return None
    conf = (1 - mad / (MAX_REL_MAD * period)) * min(1.0, len(dates) / 6)
    return Recurrence(
        category=category, counterparty=cp, period_days=period,
        day_of_month=int(np.median([d.day for d in dates])) if period == 30 else None,
        last_date=dates[-1], amount_paise=int(np.median(np.abs(amounts[-6:]))),
        confidence=float(conf), n=len(dates),
    )


def detect_recurrences(outflows: pl.DataFrame) -> list[Recurrence]:
    """outflows: value_date, amount_paise (< 0), narration_raw, category."""
    df = outflows.with_columns(
        pl.Series("cp", [stream_key(n) for n in outflows["narration_raw"]], dtype=pl.String))
    found = []
    for (cat, cp), g in df.group_by(["category", "cp"], maintain_order=True):
        r = _fit_group(cat, cp, g["value_date"].to_list(), g["amount_paise"].to_list())
        if r is not None:
            found.append(r)
    return found
