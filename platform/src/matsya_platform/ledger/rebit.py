"""Parse a ReBIT AA deposit payload into raw rows, bank-reported balances and fetch ranges.

AA model: every row and the summary of one fetch share one known_at, the fetch time.
Rows are never given individual lag. Dates derived from timestamps are IST dates.

Money becomes integer paise via Decimal, never floats. Amounts and balances may arrive as
strings or JSON numbers. ReBIT amounts are unsigned; the sign comes from the row's
`type` (CREDIT/DEBIT) applied to the amount as sent. A negative amount on a DEBIT is
accepted as is; a negative amount on a CREDIT is a sign/direction mismatch and is left for
quarantine to catch. Structural problems (unknown type, unparseable amount or date) raise
RebitError and fail the payload.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from matsya_contracts.enums import Direction

_AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")
IST = timezone(timedelta(hours=5, minutes=30))


class RebitError(ValueError):
    pass


@dataclass(frozen=True)
class Payload:
    """One raw.aa_payloads row with its decrypted JSON document."""

    tenant_id: str
    payload_id: str
    fetched_at: datetime
    doc: dict


@dataclass(frozen=True)
class RawTxn:
    tenant_id: str
    payload_id: str
    account_id: str
    index: int  # position in the payload
    bank_txn_id: str | None
    reference: str | None  # UTR or RRN when the bank fills it
    value_date: date
    posted_at: datetime  # transactionTimestamp: posting order drives running balances
    raw_amount_paise: int  # as sent, with the payload's own sign
    direction: Direction
    narration: str | None
    balance_after_paise: int | None
    known_at: datetime

    @property
    def amount_paise(self) -> int:
        """Signed: + credit, - debit. A negative raw amount keeps its sign."""
        if self.raw_amount_paise < 0:
            return self.raw_amount_paise
        return self.raw_amount_paise if self.direction is Direction.CREDIT else -self.raw_amount_paise


@dataclass(frozen=True)
class SummaryBalance:
    tenant_id: str
    payload_id: str
    account_id: str
    at: datetime  # balanceDateTime, with the bank's offset
    balance_paise: int
    known_at: datetime

    @property
    def ist_date(self) -> date:
        return self.at.astimezone(IST).date()


@dataclass(frozen=True)
class FetchRange:
    """The date range a fetch covered for one account (Transactions startDate..endDate)."""

    tenant_id: str
    payload_id: str
    account_id: str
    start: date
    end: date
    known_at: datetime


def to_paise(text: str | int | float) -> int:
    if isinstance(text, bool):
        raise RebitError(f"bad amount {text!r}")
    text = str(text).strip()
    if not _AMOUNT.match(text):
        raise RebitError(f"bad amount {text!r}")
    try:
        return int(Decimal(text) * 100)
    except InvalidOperation as e:
        raise RebitError(f"bad amount {text!r}") from e


def _ts(text: str, field: str) -> datetime:
    try:
        ts = datetime.fromisoformat(text)
    except (TypeError, ValueError) as e:
        raise RebitError(f"bad {field} {text!r}") from e
    if ts.tzinfo is None:
        raise RebitError(f"{field} {text!r} has no offset")
    return ts


def _opt(row: dict, key: str) -> str | None:
    v = row.get(key)
    return str(v).strip() or None if v is not None else None


def _accounts(doc: dict) -> list[dict]:
    acc = doc.get("Account")
    if isinstance(acc, dict):
        return [acc]
    if isinstance(acc, list):
        return acc
    raise RebitError("payload has no Account")


def _date(text, field: str) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError as e:
        raise RebitError(f"bad {field} {text!r}") from e


def parse(payload: Payload) -> tuple[list[RawTxn], list[SummaryBalance], list[FetchRange]]:
    rows, summaries, ranges = [], [], []
    for acc in _accounts(payload.doc):
        account_id = _opt(acc, "linkedAccRef")
        if account_id is None:
            raise RebitError("account has no linkedAccRef")
        block = acc.get("Transactions") or {}
        if block.get("startDate") and block.get("endDate"):
            ranges.append(FetchRange(
                tenant_id=payload.tenant_id, payload_id=payload.payload_id, account_id=account_id,
                start=_date(block["startDate"], "startDate"), end=_date(block["endDate"], "endDate"),
                known_at=payload.fetched_at,
            ))
        for i, t in enumerate(block.get("Transaction") or []):
            try:
                direction = Direction(str(t.get("type", "")).lower())
            except ValueError as e:
                raise RebitError(f"row {i}: unknown type {t.get('type')!r}") from e
            posted_at = _ts(t.get("transactionTimestamp"), "transactionTimestamp")
            vd = _opt(t, "valueDate")
            value_date = _date(vd, f"row {i} valueDate") if vd else posted_at.astimezone(IST).date()
            bal = t.get("currentBalance")
            rows.append(RawTxn(
                tenant_id=payload.tenant_id, payload_id=payload.payload_id, account_id=account_id, index=i,
                bank_txn_id=_opt(t, "txnId"), reference=_opt(t, "reference"), value_date=value_date,
                posted_at=posted_at, raw_amount_paise=to_paise(t.get("amount", "")), direction=direction,
                narration=_opt(t, "narration"),
                balance_after_paise=None if bal is None or str(bal).strip() == "" else to_paise(bal),
                known_at=payload.fetched_at,
            ))
        s = acc.get("Summary")
        if s and s.get("currentBalance") is not None and s.get("balanceDateTime"):
            summaries.append(SummaryBalance(
                tenant_id=payload.tenant_id, payload_id=payload.payload_id, account_id=account_id,
                at=_ts(s["balanceDateTime"], "balanceDateTime"), balance_paise=to_paise(s["currentBalance"]),
                known_at=payload.fetched_at,
            ))
    return rows, summaries, ranges
