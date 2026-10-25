"""Exact rupee costs of the decision engine's actions, in int64 paise.

All arithmetic is in Fractions and rounded once, half up, to whole paise. Rates come from the
constants file; the ones without a source are marked status verify there.

  TReDS discount     cash = A - A x r x days_early / 365, cost = A - cash
  overdraft draw     cost = A x r x days_used / 365
  MSMED interest     I = P x (prod_k (1 + 3 r_k / 12) - 1), one factor per monthly rest, r_k the
                     RBI bank rate in force on that rest date; an incomplete last month accrues
                     pro rata by days at the rate in force on the end date
  43B(h) deferral    tax x A / (1 + GST) x cost_of_funds x years, years = FY the deduction moves to
                     minus the FY it had (0 when a late payment stays in the same FY)
"""

import copy
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from matsya_intelligence.statutory import msmed_rate_multiplier
from matsya_intelligence.synth.world import load_constants

ENTITY_TYPES = ("proprietor", "partnership", "company")


_OVERRIDES: ContextVar[dict] = ContextVar("decision_constants_overrides", default={})


def constants() -> dict:
    """The constants file's decision section, with any overrides active in this context applied."""
    k = load_constants()["decision"]
    if not _OVERRIDES.get():
        return k
    k = copy.deepcopy(k)  # load_constants is cached: never edit its dict
    for path, v in _OVERRIDES.get().items():
        *head, last = path.split(".")
        node = k
        for part in head:
            node = node[part]
        if last not in node:
            raise KeyError(f"no decision constant {path!r}")
        if isinstance(node[last], dict) and "value" in node[last]:
            node[last]["value"] = v
        else:
            node[last] = v
    return k


@contextmanager
def overrides(values: dict | None) -> Iterator[None]:
    """Override decision constants by dotted path (e.g. {"msmed_claim.recovery_share": 0.25}) inside the block."""
    token = _OVERRIDES.set(dict(values or {}))
    try:
        constants()  # unknown paths fail here, before any work
        yield
    finally:
        _OVERRIDES.reset(token)


def frac(x) -> Fraction:
    """Exact Fraction of a config number (0.0575 means 575/10000, not its binary float)."""
    return x if isinstance(x, Fraction) else Fraction(Decimal(str(x)))


def to_paise(x: Fraction) -> int:
    """Round half up to whole paise."""
    return math.floor(x + Fraction(1, 2))


def bank_rate_history(entries: list[dict] | None = None) -> list[tuple[date, Fraction]]:
    """(effective_from, annual rate), oldest first. An entry dated TODO is taken as in force
    from the earliest date (its start is not sourced yet; see rbi_bank_rate.todo)."""
    entries = entries if entries is not None else load_constants()["rbi_bank_rate"]["history"]
    out = []
    for e in entries:
        d = date.min if str(e["effective_from"]).upper() == "TODO" else date.fromisoformat(str(e["effective_from"]))
        out.append((d, frac(e["rate"])))
    return sorted(out)


def rate_on(history: list[tuple[date, Fraction]], d: date) -> Fraction:
    rate = None
    for start, r in history:
        if start <= d:
            rate = r
    if rate is None:
        raise ValueError(f"no bank rate in force on {d}")
    return rate


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def msmed_interest(principal_paise: int, start: date, end: date,
                   history: list[tuple[date, Fraction]] | None = None) -> int:
    """MSMED s.16 compound interest on principal for the days [start, end).

    start is the appointed day (the day after the s.15 deadline). Monthly rests at start + 1, 2, ...
    months. # VERIFY: rate at each rest date and the pro rata last month (constants
    decision.msmed_claim.rate_at / partial_month).
    """
    if end <= start or principal_paise <= 0:
        return 0
    history = history if history is not None else bank_rate_history()
    mult = msmed_rate_multiplier()
    factor = Fraction(1)
    k = 0
    while add_months(start, k + 1) <= end:
        rest = add_months(start, k + 1)
        factor *= 1 + mult * rate_on(history, rest) / 12
        k += 1
    p_start = add_months(start, k)
    if p_start < end:
        p_end = add_months(start, k + 1)
        share = Fraction((end - p_start).days, (p_end - p_start).days)
        factor *= 1 + mult * rate_on(history, end) / 12 * share
    return to_paise(principal_paise * (factor - 1))


def treds_discount(amount_paise: int, rate_annual, days_early: int) -> tuple[int, int]:
    """(cash received, cost) for discounting amount days_early days before its due date."""
    cost = to_paise(amount_paise * frac(rate_annual) * max(days_early, 0) / 365)
    return amount_paise - cost, cost


def overdraft_interest(amount_paise: int, rate_annual, days_used: int) -> int:
    return to_paise(amount_paise * frac(rate_annual) * max(days_used, 0) / 365)


def income_tax_rate(entity_type: str | None, owner_rate=None) -> Fraction | None:
    """The owner's rate if given, else the constants default for the entity type (company only).
    None: no rate known (a proprietor or partnership that has not given one)."""
    if owner_rate is not None:
        return frac(owner_rate)
    if entity_type is None:
        return None
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {ENTITY_TYPES}, got {entity_type!r}")
    v = constants()["stretch"]["income_tax_rate"][entity_type]["value"]  # VERIFY: company 25.17%
    return None if v is None else frac(v)


def deduction_deferral_cost(amount_years_paise, tax_rate: Fraction, cost_of_funds) -> int:
    """Cost of a 43B(h) / 37(2)(g) deduction moved to a later FY: the tax saving arrives later by
    that many years, valued at the cost of funds. amount_years_paise = sum of amount (net of GST) x years."""
    return to_paise(frac(amount_years_paise) * tax_rate * frac(cost_of_funds))


def fiscal_year(d: date) -> int:
    """Indian FY by its starting year (1 Apr - 31 Mar)."""
    return d.year if d.month >= 4 else d.year - 1
