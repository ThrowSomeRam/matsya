"""Deduplication: an exact pass on txn_uid, then a fuzzy pass resolved by continuity.

Exact: a txn_uid already in the ledger (or earlier in the batch) is a no-op. A bank txnId
seen before with a different date, amount or narration raises a txn_id_conflict event; if
its txn_uid differs both rows are kept.

Fuzzy (ordinal-keyed rows only): a row is a suspected redelivery of an earlier row when
both are on the same account with the same signed amount, value dates at most one day
apart, narration similarity at least SIMILARITY, and they come from different payloads.
Rows inside one payload are never duplicates of each other. Every keep/drop choice over the
suspects is scored by continuity breaks; a unique best choice is applied. Otherwise every
suspect is kept and flagged with dup_group_id and a lowered dq_score: nothing is dropped
without evidence.
"""

import itertools
import re
from dataclasses import dataclass, replace
from datetime import datetime

from matsya_contracts import ids as cids
from matsya_contracts import tables as T
from matsya_contracts.enums import Direction, EventKind, TxnSource
from matsya_contracts.events import AcceptGap, TxnIdConflict
from matsya_platform.ledger.continuity import walk
from matsya_platform.ledger.ids import Keyed, KeyPath
from matsya_platform.ledger.rebit import SummaryBalance

SIMILARITY = 0.6
MAX_SUSPECTS = 8  # 2**8 continuity walks per batch at most
DUP_SUSPECT_DQ = 0.5


@dataclass(frozen=True)
class LedgerRow:
    """A ledger.transactions row plus what A4 needs to keep about it (not in the contract)."""

    txn: T.Transaction
    key_path: KeyPath
    bank_txn_id: str | None
    payload_id: str
    index: int
    posted_at: datetime
    balance_after_paise: int | None

    account_id = property(lambda self: self.txn.account_id)
    amount_paise = property(lambda self: self.txn.amount_paise)
    value_date = property(lambda self: self.txn.value_date)
    known_at = property(lambda self: self.txn.known_at)
    txn_uid = property(lambda self: self.txn.txn_uid)

    def arrival(self):
        return (self.known_at, self.payload_id, self.index)


def to_row(k: Keyed, dq_score: float = 1.0) -> LedgerRow:
    raw = k.raw
    amount = raw.amount_paise
    txn = T.Transaction(
        tenant_id=raw.tenant_id, txn_uid=k.txn_uid, account_id=raw.account_id, value_date=raw.value_date,
        known_at=raw.known_at, amount_paise=amount, direction=Direction.CREDIT if amount > 0 else Direction.DEBIT,
        narration_raw=raw.narration, source=TxnSource.AA, dq_score=dq_score, dup_group_id=None,
    )
    return LedgerRow(txn=txn, key_path=k.key_path, bank_txn_id=raw.bank_txn_id, payload_id=raw.payload_id,
                     index=raw.index, posted_at=raw.posted_at, balance_after_paise=raw.balance_after_paise)


def exact(keyed: list[Keyed], existing: tuple[LedgerRow, ...]) -> tuple[list[LedgerRow], list[T.Event]]:
    known = {r.txn_uid for r in existing}
    by_bank_id: dict[tuple[str, str], list[LedgerRow]] = {}
    for r in existing:
        if r.bank_txn_id:
            by_bank_id.setdefault((r.account_id, r.bank_txn_id), []).append(r)
    fresh, events = [], []
    for k in sorted(keyed, key=lambda k: (k.raw.known_at, k.raw.payload_id, k.raw.index)):
        row = to_row(k)
        if row.bank_txn_id:
            prior = by_bank_id.setdefault((row.account_id, row.bank_txn_id), [])
            other = next((p for p in prior if _fields(p) != _fields(row)), None)
            if other is not None:
                events.append(_conflict(other, row))
            if row.txn_uid not in known:
                prior.append(row)
        if row.txn_uid in known:
            continue
        known.add(row.txn_uid)
        fresh.append(row)
    return fresh, events


def _fields(r: LedgerRow):
    return (r.value_date, r.amount_paise, cids.normalise_party(r.txn.narration_raw or ""))


def _conflict(existing: LedgerRow, new: LedgerRow) -> T.Event:
    p = TxnIdConflict(
        account_id=new.account_id, bank_txn_id=new.bank_txn_id, existing_txn_uid=existing.txn_uid,
        payload_id=new.payload_id, existing_value_date=existing.value_date, new_value_date=new.value_date,
        existing_amount_paise=existing.amount_paise, new_amount_paise=new.amount_paise,
    )
    return T.Event(tenant_id=new.txn.tenant_id,
                   event_id=cids.hash32("event", "txn_id_conflict", existing.txn_uid, new.payload_id, str(new.index)),
                   kind=EventKind.TXN_ID_CONFLICT, payload_json=p.model_dump_json(), at=new.known_at)


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.split(r"[^A-Z0-9]+", cids.normalise_party(text or "")) if t}


def similarity(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 1.0


def _is_candidate(suspect: LedgerRow, other: LedgerRow) -> bool:
    return (other.payload_id != suspect.payload_id and other.account_id == suspect.account_id
            and other.amount_paise == suspect.amount_paise
            and abs((other.value_date - suspect.value_date).days) <= 1
            and similarity(other.txn.narration_raw, suspect.txn.narration_raw) >= SIMILARITY)


def suspects(fresh: list[LedgerRow], existing: tuple[LedgerRow, ...]) -> list[tuple[LedgerRow, LedgerRow]]:
    """(suspect, earlier row it may duplicate) pairs. Only ordinal-keyed fresh rows are suspects."""
    pairs = []
    earlier = list(existing)
    for row in sorted(fresh, key=LedgerRow.arrival):
        if row.key_path == "ord":
            matches = [o for o in earlier if _is_candidate(row, o)]
            if matches:  # most similar; ties go to the earliest arrival
                pairs.append((row, max(matches, key=lambda o: similarity(o.txn.narration_raw, row.txn.narration_raw))))
        earlier.append(row)
    return pairs


def resolve(fresh: list[LedgerRow], existing: tuple[LedgerRow, ...], summaries: tuple[SummaryBalance, ...],
            gaps: tuple[AcceptGap, ...]) -> list[LedgerRow]:
    pairs = suspects(fresh, existing)
    if not pairs:
        return fresh
    scored = pairs[:MAX_SUSPECTS]
    scores = []
    for mask in itertools.product((False, True), repeat=len(scored)):
        dropped = {p[0].txn_uid for p, drop in zip(scored, mask) if drop}
        rows = list(existing) + [r for r in fresh if r.txn_uid not in dropped]
        scores.append((sum(len(w.breaks) for w in walk(rows, summaries, gaps).values()), dropped))
    best = min(s for s, _ in scores)
    winners = [d for s, d in scores if s == best]
    if len(winners) == 1 and len(pairs) <= MAX_SUSPECTS:
        return [r for r in fresh if r.txn_uid not in winners[0]]
    return _flag(fresh, pairs)


def _flag(fresh: list[LedgerRow], pairs: list[tuple[LedgerRow, LedgerRow]]) -> list[LedgerRow]:
    group = {}
    for suspect, match in pairs:
        gid = cids.hash32("dup", *sorted((suspect.txn_uid, match.txn_uid)))
        group[suspect.txn_uid] = gid
        group.setdefault(match.txn_uid, gid)
    out = []
    for r in fresh:
        if r.txn_uid in group:
            r = replace(r, txn=r.txn.model_copy(update={"dup_group_id": group[r.txn_uid],
                                                        "dq_score": min(r.txn.dq_score, DUP_SUSPECT_DQ)}))
        out.append(r)
    return out
