"""Balance continuity: walk each account in posting order and compare against bank anchors.

Anchors are a row's post-transaction running balance and a payload's Summary balance at
balanceDateTime. With c the running sum of signed amounts, each anchor v implies an
opening balance v - c. The first anchor fixes the opening; any later anchor implying a
different one is a break of gap = implied - current (bank minus reconstructed), after
which the walk re-anchors. Rows before the first anchor cannot be checked.

A break is dated by the IST posting date of the anchor that reveals it (a row's
transactionTimestamp, or the Summary's balanceDateTime). The missing money arose in posting
order, after the last consistent anchor and by the revealing one, so its value date is
unknown: every date from the earlier of that last anchor's posting date and the value dates
of the rows posted since, through the break, is the break's uncertainty window.

A break is accepted only by an accept_gap with the same account, a date window covering
the break's posting date, and the same gap in paise.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from matsya_contracts.events import AcceptGap
from matsya_platform.ledger.rebit import IST, FetchRange, SummaryBalance

if TYPE_CHECKING:
    from matsya_platform.ledger.dedup import LedgerRow


@dataclass(frozen=True)
class Break:
    account_id: str
    on: date  # IST posting date of the revealing anchor
    gap_paise: int  # bank-reported minus reconstructed
    accepted_by: AcceptGap | None = None
    window_from: date | None = None  # first date the missing money could belong to

    @property
    def accepted(self) -> bool:
        return self.accepted_by is not None


@dataclass(frozen=True)
class Walk:
    opening_paise: int | None  # None: no anchor, the absolute balance is unknown
    breaks: list[Break] = field(default_factory=list)


class ContinuityError(Exception):
    def __init__(self, breaks: list[Break]):
        self.breaks = breaks
        super().__init__("; ".join(f"{b.account_id} {b.on}: gap {b.gap_paise} paise" for b in breaks))


def _accepting(account_id: str, on: date, gap: int, gaps: tuple[AcceptGap, ...]) -> AcceptGap | None:
    for g in gaps:
        if g.account_id == account_id and g.from_date <= on <= g.to_date and g.gap_paise == gap:
            return g
    return None


def walk(rows: "list[LedgerRow] | tuple[LedgerRow, ...]", summaries: tuple[SummaryBalance, ...],
         gaps: tuple[AcceptGap, ...] = ()) -> dict[str, Walk]:
    items: dict[str, list] = {}
    for r in rows:
        items.setdefault(r.account_id, []).append(((r.posted_at, 0, r.known_at, r.payload_id, r.index), r))
    for s in summaries:
        items.setdefault(s.account_id, []).append(((s.at, 1, s.known_at, s.payload_id, 0), s))
    out = {}
    for account_id, seq in items.items():
        seq.sort(key=lambda kv: kv[0])
        c, opening, current, breaks = 0, None, None, []
        last_good, min_vd = None, None  # posting date of the last consistent anchor; value dates since
        for _, item in seq:
            if isinstance(item, SummaryBalance):
                anchor, on = item.balance_paise, item.ist_date
            else:
                c += item.amount_paise
                anchor, on = item.balance_after_paise, item.posted_at.astimezone(IST).date()
                min_vd = item.value_date if min_vd is None else min(min_vd, item.value_date)
            if anchor is None:
                continue
            implied = anchor - c
            if opening is None:
                opening = current = implied
            elif implied != current:
                gap = implied - current
                start = min(d for d in (last_good, min_vd, on) if d is not None)
                breaks.append(Break(account_id, on, gap, _accepting(account_id, on, gap, gaps), start))
                current = implied
            last_good, min_vd = on, None
        out[account_id] = Walk(opening, breaks)
    return out


@dataclass(frozen=True)
class Coverage:
    tenant_id: str
    account_id: str
    complete_through: date  # end of the unbroken run of fetched ranges from the first fetch
    known_at: datetime  # the latest fetch inside that run


def coverage(ranges: tuple[FetchRange, ...]) -> list[Coverage]:
    """Per account, how far the ledger is complete. Built from fetch ranges only, so a
    backdated row (posted late, value-dated weeks earlier) never moves it. A gap between
    ranges stops it until a fetch fills the gap."""
    by_account: dict[str, list[FetchRange]] = {}
    for r in ranges:
        by_account.setdefault(r.account_id, []).append(r)
    out = []
    for account_id, rs in sorted(by_account.items()):
        rs.sort(key=lambda r: (r.start, r.end))
        end, known = rs[0].end, rs[0].known_at
        for r in rs[1:]:
            if r.start > end + timedelta(days=1):
                break
            if r.end > end:
                end = r.end
            known = max(known, r.known_at)
        out.append(Coverage(rs[0].tenant_id, account_id, end, known))
    return out
